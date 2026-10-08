"""研究型入口持久化服务（feat-research-entry Task 3）。

承载 5 张研究表的写入链路：research_session / research_turn /
research_checkpoint / research_finding / research_report。

约定：
- 服务无状态，所有方法首参为 `session: AsyncSession`；
- **不自行 commit**：事务归调用方（FastAPI 依赖注入的 session）所有，
  本服务只 `flush` 让自增列/默认值可见（与 acl_service / agent_runtime_service 一致）；
- checkpoint 的 `prompt` 无独立列，随 `options["prompt"]` 一并落 JSONB；
- 非法入参（状态白名单、confidence 越界、目标行不存在）先 `logger` 记录上下文再显式抛错；
- 缺会话 / 缺轮次的写入（appendTurn / openCheckpoint / saveFinding / publishReport）
  刻意不做存在性预检：由 FK 约束在 `flush` 时以 `IntegrityError` 冒泡（省一次 SELECT，
  且这些写入的正常前置必然是先 createSession）。此为有意取舍，非静默吞错。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditLog
from app.domain.research_models import (
    ResearchCheckpoint,
    ResearchFinding,
    ResearchReport,
    ResearchSession,
    ResearchTurn,
)
from app.services.research_agent_ports import DEFAULT_MODE  # re-export for backward compat

logger = logging.getLogger(__name__)

CHECKPOINT_PENDING = "pending"
REPORT_PUBLISHED = "published"
REPORT_SUPERSEDED = "superseded"
CHECKPOINT_STATUSES = frozenset({"confirmed", "modified", "rejected"})
"""用户决策的合法取值（设计 §2 表 DDL：pending | confirmed | modified | rejected）。

显式白名单而非透传：若允许 `status="pending"`，会把已决策的 checkpoint 重新「挂起」，
`getPendingCheckpoint` 又返回它 ⇒ 重复决策洞。
"""
SESSION_STATUSES = frozenset({"running", "awaiting_user", "done", "failed", "aborted"})
"""会话状态合法取值（设计 §2 表 DDL：running | awaiting_user | done | failed | aborted）。"""
PROMPT_KEY = "prompt"
"""checkpoint 无 prompt 列，提示文案随 options 落 JSONB 的键名（Task 5 读同键）。"""
TITLE_MAX_LEN = 200
"""会话标题取问题前 N 字（title 列无长度约束，此处为可读性截断）。"""


class CheckpointStaleError(ValueError):
    """checkpoint 已过期（expires_at <= now），不能再做决策。"""


class CheckpointConflictError(ValueError):
    """checkpoint 状态不是 pending（已被决策或不存在）。"""


class ResearchSessionService:
    """研究会话持久化（无状态）。"""

    async def createSession(
        self,
        session: AsyncSession,
        *,
        userId: int | None,
        question: str,
        mode: str = DEFAULT_MODE,
        datasourceId: int | None = None,
        modelId: int | None = None,
    ) -> ResearchSession:
        """新建会话；`input_seed` 存原始问题（重启后据此恢复意图）。

        `datasourceId`（Task 13e）由 API 层解析后传入（显式 id 或默认源）：执行面据此
        连业务库。**不在此处解析默认源** —— 本服务不碰数据源域（单一职责），且解析
        失败必须在建会话之前显式发生（不留无源的半成品会话）。

        `modelId`（W5）同理由 API 层解析校验后传入（None = 自动路由）：本服务不碰模型
        配置域。
        """
        row = ResearchSession(
            created_by=userId,
            title=question[:TITLE_MAX_LEN],
            input_seed=question,
            mode=mode,
            datasource_id=datasourceId,
            model_id=modelId,
        )
        session.add(row)
        await session.flush()
        logger.info("创建研究会话: id=%s userId=%s mode=%s", row.id, userId, mode)
        return row

    async def deleteSession(self, session: AsyncSession, sessionId: uuid.UUID) -> int:
        """硬删会话主行，返回删除行数（0 = 不存在）。

        级联由 DB 承担：`research_models.py:_session_fk()` 的 `ondelete="CASCADE"`
        会一并清 turn / checkpoint / finding / report —— 无需在应用层逐表删。

        用 `RETURNING` 计数而非 `result.rowcount`：asyncpg 对 DELETE 的 rowcount
        可能返回 -1（与 chat 的 `session_history_service.deleteSessionHistory`
        同一约定）。**不 commit** —— 提交由调用方（router）统一负责，与
        `createSession` 的 flush-only 风格一致。

        **归属不在本方法校验**（2026-10-05 安全审查 MEDIUM 更正）：唯一调用方
        `api/v1/research.py` 的 `deleteSession` 路由会先过 `_ownedSession`
        （越权 ⇒ 404，不泄露存在性），那是本操作的**唯一**闸门。此处**刻意**不加
        `created_by` 谓词 —— 会话 id 是全局唯一 UUID 且本方法没有第二个调用点。
        **新增调用方必须自行校验归属**，不要指望本方法兜底：硬删不可逆。
        """
        rows = await session.scalars(
            delete(ResearchSession)
            .where(ResearchSession.id == sessionId)
            .returning(ResearchSession.id)
        )
        return len(rows.all())

    async def appendTurn(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        role: str,
        content: dict[str, Any],
    ) -> ResearchTurn:
        """追加一轮交互，`turn_index` 取会话内 max+1（首轮为 0）。

        研究会话单人串行，并发低，不加锁；靠 `flush` 保证同会话连续追加不重号。
        """
        nextIndex = await session.scalar(
            select(func.coalesce(func.max(ResearchTurn.turn_index), -1) + 1).where(
                ResearchTurn.session_id == sessionId
            )
        )
        row = ResearchTurn(
            session_id=sessionId, turn_index=nextIndex, role=role, content=content or {}
        )
        session.add(row)
        await session.flush()
        return row

    async def openCheckpoint(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        turnId: uuid.UUID,
        phase: str,
        options: dict[str, Any],
        prompt: str,
        expiresAt: datetime | None = None,
    ) -> ResearchCheckpoint:
        """挂起一个待决策检查点（status=pending），等待用户选择。"""
        row = ResearchCheckpoint(
            session_id=sessionId,
            turn_id=turnId,
            phase=phase,
            status=CHECKPOINT_PENDING,
            options={**(options or {}), PROMPT_KEY: prompt},
            expires_at=expiresAt,
        )
        session.add(row)
        await session.flush()
        logger.info("开启 checkpoint: id=%s session=%s phase=%s", row.id, sessionId, phase)
        return row

    async def resolveCheckpoint(
        self,
        session: AsyncSession,
        *,
        checkpointId: uuid.UUID,
        status: str,
        userChoice: dict[str, Any],
        userId: int | None = None,
    ) -> ResearchCheckpoint:
        """写入用户决策；非法状态或非 pending 一律拒绝（幂等保护，防重复提交）。

        Task 7.5 MEDIUM：判据是**原子条件 UPDATE**（`WHERE status='pending'`），
        rowcount=0 即拒绝。此前的「先读后写」是 TOCTOU：两个并发 answer 都读到
        pending ⇒ 双双放行 ⇒ 状态机双跑（重复 execute / verify / 报告归档）。
        异常类型与文案语义不变（仍是 ValueError，仍区分「不存在」与「非 pending」）。

        B5：决策成功后写 audit_log（同事务），便于长期审计。
        """
        if status not in CHECKPOINT_STATUSES:
            logger.warning("非法 checkpoint 决策状态: id=%s status=%s", checkpointId, status)
            raise ValueError(f"非法 checkpoint 状态 {status}，合法值: {sorted(CHECKPOINT_STATUSES)}")
        row = await session.get(ResearchCheckpoint, checkpointId)
        if row is None:
            logger.warning("checkpoint 不存在: id=%s", checkpointId)
            raise ValueError(f"checkpoint 不存在: {checkpointId}")
        result = await session.execute(
            update(ResearchCheckpoint)
            .where(
                ResearchCheckpoint.id == checkpointId,
                ResearchCheckpoint.status == CHECKPOINT_PENDING,
                or_(
                    ResearchCheckpoint.expires_at.is_(None),
                    ResearchCheckpoint.expires_at > datetime.now(UTC),
                ),
            )
            .values(status=status, user_choice=userChoice or {}, decided_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            await session.refresh(row)  # 刷新以报出**库中真实**状态（identity map 可能陈旧）
            if row.status == CHECKPOINT_PENDING and (
                row.expires_at is not None and row.expires_at <= datetime.now(UTC)
            ):
                logger.warning("checkpoint 已过期，拒绝决策: id=%s", checkpointId)
                raise CheckpointStaleError(f"checkpoint 已过期: {checkpointId}")
            logger.warning(
                "checkpoint 非 pending，拒绝重复决策: id=%s status=%s", checkpointId, row.status
            )
            raise CheckpointConflictError(f"checkpoint 非 pending（当前 {row.status}）: {checkpointId}")
        await session.flush()
        await session.refresh(row)
        # B5：audit_log 与 UPDATE 同事务（AuditLog 不可变，仅 INSERT）
        # checkpoint.id 是 UUID，audit_log.entity_id 是 BIGINT（不兼容），
        # 走 entity_id_str 列；entity_id 留 NULL（0117 起允许 NULL）。
        session.add(AuditLog(
            entity_type="research_checkpoint",
            entity_id=None,
            entity_id_str=str(row.id),
            action="UPDATE",
            actor=str(userId) if userId is not None else "",
            before_json=None,
            after_json={
                "status": status,
                "user_choice": userChoice,
                "session_id": str(row.session_id),
            },
        ))
        await session.flush()
        return row

    async def getPendingCheckpoint(
        self, session: AsyncSession, sessionId: uuid.UUID
    ) -> ResearchCheckpoint | None:
        """取会话内最新一个待决策检查点。

        排序 = 所属轮次 `turn_index` 倒序，再以 `id` 兜底成全序（checkpoint 表无
        created_at，同轮次多条 pending 时 `id` 是唯一可用的稳定次序键；设计上
        一轮至多开一个 pending，见设计 §4.4）。
        """
        stmt = (
            select(ResearchCheckpoint)
            .join(ResearchTurn, ResearchTurn.id == ResearchCheckpoint.turn_id)
            .where(
                ResearchCheckpoint.session_id == sessionId,
                ResearchCheckpoint.status == CHECKPOINT_PENDING,
                or_(
                    ResearchCheckpoint.expires_at.is_(None),
                    ResearchCheckpoint.expires_at > datetime.now(UTC),
                ),
            )
            .order_by(ResearchTurn.turn_index.desc(), ResearchCheckpoint.id.desc())
            .limit(1)
        )
        return await session.scalar(stmt)

    async def saveFinding(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        turnId: uuid.UUID,
        claimText: str,
        supportingSql: str | None,
        supportingData: dict[str, Any],
        confidence: float,
    ) -> ResearchFinding:
        """落一条结论（含支撑 SQL / 数据 / 置信度）。"""
        if not 0.0 <= confidence <= 1.0:
            logger.warning("置信度越界: session=%s confidence=%s", sessionId, confidence)
            raise ValueError(f"confidence 必须落在 [0, 1]: {confidence}")
        row = ResearchFinding(
            session_id=sessionId,
            turn_id=turnId,
            claim_text=claimText,
            supporting_sql=supportingSql,
            supporting_data=supportingData or {},
            confidence=Decimal(str(confidence)),
        )
        session.add(row)
        await session.flush()
        return row

    async def publishReport(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        payload: dict[str, Any],
        renderedMd: str,
    ) -> ResearchReport:
        """发布报告：旧 published 置 superseded，版本号 max+1，单事务内完成。

        UPDATE 后立即 `flush`，避免部分唯一索引（每会话仅 1 个 published）
        在新版本 INSERT 时与旧行冲突。
        """
        await session.execute(
            update(ResearchReport)
            .where(
                ResearchReport.session_id == sessionId,
                ResearchReport.status == REPORT_PUBLISHED,
            )
            .values(status=REPORT_SUPERSEDED)
            .execution_options(synchronize_session="fetch")
        )
        await session.flush()
        nextVersion = await session.scalar(
            select(func.coalesce(func.max(ResearchReport.version), 0) + 1).where(
                ResearchReport.session_id == sessionId
            )
        )
        row = ResearchReport(
            session_id=sessionId,
            version=nextVersion,
            payload=payload or {},
            rendered_md=renderedMd,
            status=REPORT_PUBLISHED,
        )
        session.add(row)
        await session.flush()
        logger.info("发布研究报告: session=%s version=%s", sessionId, nextVersion)
        return row

    async def listReports(
        self, session: AsyncSession, sessionId: uuid.UUID
    ) -> list[ResearchReport]:
        """按版本升序列出会话全部报告（含历史版本）。"""
        rows = await session.scalars(
            select(ResearchReport)
            .where(ResearchReport.session_id == sessionId)
            .order_by(ResearchReport.version.asc())
        )
        return list(rows)

    async def listFindings(
        self, session: AsyncSession, sessionId: uuid.UUID
    ) -> list[ResearchFinding]:
        """按产生顺序列出会话全部结论（报告装配的数据来源，含行数据）。

        读侧唯一入口：`ReportPlanner` 不直接查 `research_finding` 表，
        保证 Task 5 写契约（`supporting_data["rows"]`）与 Task 6 读契约同源。
        """
        rows = await session.scalars(
            select(ResearchFinding)
            .where(ResearchFinding.session_id == sessionId)
            .order_by(ResearchFinding.created_at.asc(), ResearchFinding.id.asc())
        )
        return list(rows)

    async def updateSessionStatus(
        self, session: AsyncSession, sessionId: uuid.UUID, status: str
    ) -> None:
        """推进会话状态（running / awaiting_user / done / failed / aborted）。"""
        if status not in SESSION_STATUSES:
            logger.warning("非法会话状态: id=%s status=%s", sessionId, status)
            raise ValueError(f"非法会话状态 {status}，合法值: {sorted(SESSION_STATUSES)}")
        row = await session.get(ResearchSession, sessionId)
        if row is None:
            logger.warning("会话不存在，无法更新状态: id=%s", sessionId)
            raise ValueError(f"会话不存在: {sessionId}")
        row.status = status
        await session.flush()
        logger.info("会话状态更新: id=%s status=%s", sessionId, status)
