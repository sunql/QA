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

import json
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
    ResearchRunState,
    ResearchSession,
    ResearchTurn,
)
from app.services.research_agent_ports import (
    DEFAULT_MODE,  # re-export for backward compat
    STATUS_AWAITING,
)

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
TERMINAL_SESSION_STATUSES = frozenset({"done", "failed", "aborted"})
"""会话终态（Task 2）：**只有终态才释放 active_run**。

`awaiting_user` 不是终态 —— 运行只是暂停等用户决策，同一轮次还在继续，执行权必须保留，
否则并发提交会溜进来（互斥失效）。
"""
RUN_STATE_IDEMPOTENCY_KEY = "idempotencyKey"
"""幂等键在 `ResearchRunState.state_payload` 里的键名（Task 2）。

写入方 = 状态机入口（`ResearchAgentService.runTurn`）；读取方 = 本服务的
`getRunStateByIdempotencyKey`（重试回放同一 turnId）。**两处必须同键**。
"""


def _coerceJsonSafe(obj: Any) -> Any:
    """把不可 JSON 序列化的值（datetime / Decimal / bytes）转成 JSONB 可接受的形态。

    与项目 `json.dumps(..., default=str)` 既有约定一致（agent_tools_nl2sql /
    chat_service / report_template_service 等 5+ 处）。本实现走自定义递归而非
    ``default=str``，原因：``default=str`` 会把 datetime 渲成 ``"2026-10-09
    12:30:45+00:00"`` 这种**含空格**字符串，前端 Date 解析常踩；改用
    ``datetime.isoformat()`` 显式给 ISO 8601（与 chat_service._dumpTimestamp
    渲染口径一致）。

    Decimal → float：evidence_record_service 等多处用 ``Decimal`` 算 sha256，
    入库后转 float 不影响 evidence 校验（同一 Decimal 与其 float 化值在
    sha256 之外无对照场景）。
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return obj.hex()
    if isinstance(obj, dict):
        return {str(k): _coerceJsonSafe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerceJsonSafe(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [_coerceJsonSafe(v) for v in obj]
    # 兜底：调 json.dumps(default=str) 渲成字符串（与项目惯例一致）
    try:
        return json.loads(json.dumps(obj, default=str, ensure_ascii=False))
    except Exception:
        return str(obj)
PROMPT_KEY = "prompt"
"""checkpoint 无 prompt 列，提示文案随 options 落 JSONB 的键名（Task 5 读同键）。"""
TITLE_MAX_LEN = 200
"""会话标题取问题前 N 字（title 列无长度约束，此处为可读性截断）。"""


class CheckpointStaleError(ValueError):
    """checkpoint 已过期（expires_at <= now），不能再做决策。"""


class CheckpointConflictError(ValueError):
    """checkpoint 状态不是 pending（已被决策或不存在）。"""


class RunClaimConflictError(RuntimeError):
    """会话已有进行中的运行，本轮**未获执行权**（Task 2）。

    与 `CheckpointConflictError` 分开：那是「同一 checkpoint 被重复决策」，这是
    「同一会话被两条状态机抢」。调用方（后台 wrapper）对二者的收尾不同 ——
    本异常**不得**把会话标 failed（另一条运行正在正常跑，标 failed 会毁掉它的 UI）。
    """


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

    async def getRunStateByTurn(
        self, session: AsyncSession, turnId: uuid.UUID
    ) -> ResearchRunState | None:
        """取某一轮次最新一个 ResearchRunState（按创建时间倒序）。"""
        stmt = (
            select(ResearchRunState)
            .where(ResearchRunState.turn_id == turnId)
            .order_by(ResearchRunState.created_at.desc(), ResearchRunState.id.desc())
            .limit(1)
        )
        return await session.scalar(stmt)

    async def getRunStateByIdempotencyKey(
        self, session: AsyncSession, sessionId: uuid.UUID, idempotencyKey: str
    ) -> ResearchRunState | None:
        """按幂等键取会话内**最早**那个运行（重试回放同一 turnId 的数据来源）。

        幂等键落在 `state_payload`（JSONB，`@>` 包含查询）。版本化会复制同一 payload，
        故同键可能有多行 —— 取最早一行（`created_at`, `id` 兜底）即本轮**首次**提交的
        运行，与「重试返回同一 turnId」的语义对齐。
        """
        stmt = (
            select(ResearchRunState)
            .where(
                ResearchRunState.session_id == sessionId,
                ResearchRunState.state_payload.contains(
                    {RUN_STATE_IDEMPOTENCY_KEY: idempotencyKey}
                ),
            )
            .order_by(ResearchRunState.created_at.asc(), ResearchRunState.id.asc())
            .limit(1)
        )
        return await session.scalar(stmt)

    async def claimActiveRun(
        self,
        session: AsyncSession,
        sessionId: uuid.UUID,
        runId: uuid.UUID,
        idempotencyKey: str | None,
    ) -> tuple[bool, uuid.UUID | None]:
        """原子认领会话执行权。返回 `(success, existingRunId)`。

        - 幂等键命中**已存在的其他运行** ⇒ 视为同一运行，返回 `(True, 该运行 id)`，
          不做占用判定（重试不得被自己的首次提交挡住）；
        - 认领本身是**条件 UPDATE**（`WHERE active_run_id IS NULL`），rowcount=1 即成功 ——
          先读后写是 TOCTOU：两条并发提交都读到 NULL ⇒ 双双放行 ⇒ 两条状态机各跑一份
          （同 `resolveCheckpoint` 的原子条件 UPDATE 口径）；
        - 认领方为本运行（同 runId 重入）也放行：同一条运行的续跑不是并发。

        幂等键命中必须**排除 runId 自身**：调用方（`runTurn`）先把本轮的 run state 落库
        （带 key）再来认领，同一事务里它就可见 —— 不排除会让每一条带 key 的轮次都
        「命中自己」而跳过占用判定，互斥直接失效。

        **不 commit** —— 事务归调用方（与其余方法一致）。
        """
        if idempotencyKey:
            hit = await self.getRunStateByIdempotencyKey(session, sessionId, idempotencyKey)
            if hit is not None and hit.id != runId:
                logger.info(
                    "幂等键命中已落库运行: session=%s key=%s run=%s",
                    sessionId,
                    idempotencyKey,
                    hit.id,
                )
                return True, hit.id
        result = await session.execute(
            update(ResearchSession)
            .where(ResearchSession.id == sessionId, ResearchSession.active_run_id.is_(None))
            .values(active_run_id=runId)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await self._syncSessionRow(session, sessionId, activeRunId=runId)
            return True, None
        # 未抢到：报出**库中真实**占用者（identity map 可能陈旧，故走列查询而非 ORM 对象）
        existing = await session.scalar(
            select(ResearchSession.active_run_id).where(ResearchSession.id == sessionId)
        )
        if existing is None:
            logger.warning("认领失败但会话不存在: session=%s run=%s", sessionId, runId)
            return False, None
        if existing == runId:
            await self._syncSessionRow(session, sessionId, activeRunId=runId)
            return True, None
        logger.info("会话已被其他运行占用: session=%s run=%s existing=%s", sessionId, runId, existing)
        return False, existing

    async def releaseActiveRun(
        self, session: AsyncSession, sessionId: uuid.UUID, runId: uuid.UUID | None
    ) -> None:
        """释放执行权；**仅会话终态才真释放**。

        判据是会话状态而非 `active_run_id == runId`：运行状态在 resume 时会被版本化
        （`updateRunState` 生成新 UUID），认领时的 runId 与收尾时的 `runStateId` 未必相等；
        若按 id 相等判，版本化过的轮次永远释放不掉 ⇒ 会话被永久锁死。会话终态是
        「可以接手」的唯一可靠判据（`awaiting_user` 只是暂停，见 `TERMINAL_SESSION_STATUSES`）。

        `runId` 仅用于日志对账。**不 commit**。

        释放同样走**条件 UPDATE**（`status IN 终态`）：认领是条件 UPDATE，
        ORM 对象上的 `active_run_id` 可能陈旧（`synchronize_session=False`），
        按对象值决定「要不要清」会说谎 —— 实测表现为「库里有占用者、对象上是 None
        ⇒ 赋 None 不算脏 ⇒ 一条 UPDATE 都不发 ⇒ 数据库里的执行权永远留着，
        终态会话被锁死」。状态判定也一并下沉到 SQL（与认领同口径）。
        """
        result = await session.execute(
            update(ResearchSession)
            .where(
                ResearchSession.id == sessionId,
                ResearchSession.status.in_(TERMINAL_SESSION_STATUSES),
                ResearchSession.active_run_id.is_not(None),
            )
            .values(active_run_id=None)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await self._syncSessionRow(session, sessionId, activeRunId=None)
            logger.info("释放 active_run: session=%s run=%s", sessionId, runId)
            return
        # rowcount=0：非终态（暂停中，执行权必须留着）或会话不存在 —— 都只留痕，不报错
        status = await session.scalar(
            select(ResearchSession.status).where(ResearchSession.id == sessionId)
        )
        if status is None:
            logger.warning("释放 active_run 失败：会话不存在: session=%s", sessionId)
        else:
            logger.debug(
                "非终态（%s）保留执行权: session=%s run=%s", status, sessionId, runId
            )

    async def failSession(self, session: AsyncSession, sessionId: uuid.UUID) -> None:
        """会话落 `failed` 终态**并**释放执行权（单条 UPDATE，见 `_setTerminalAndRelease`）。"""
        await self._setTerminalAndRelease(session, sessionId, "failed")

    async def abortSessionIfActive(
        self, session: AsyncSession, sessionId: uuid.UUID
    ) -> str | None:
        """取消**停在检查点等待用户决策**（`awaiting_user`）的会话，并释放执行权。

        返回库中**真实**状态（未命中时走列查询，绕过 identity map）；行已不存在 ⇒ `None`。
        **不 commit**。调用方按返回值分支：`aborted` = 本次真的取消了；`running` = 拒绝；
        终态字面量 = 幂等无副作用的空操作。

        **白名单只有 `awaiting_user`**（review Fix A）。`active_run_id` 非空 + `awaiting_user`
        是唯一「占用在手、却**没有**任何任务在跑」的状态，也正是「不取消就永久 409」的那个
        状态。放宽成「一切非终态」是**危险**的，两条后果都会打穿本任务：

        1. 真在执行的 `running` 会话，状态机的事后写入是**无条件**的（`updateSessionStatus`
           盲写，`_runFrom` 落 done / `_pauseForUser` 落 awaiting_user）⇒ abort 之后它照样
           把状态写回去，用户看到「已取消」、刷新又活过来；
        2. 更糟：abort 顺手清空 `active_run_id` ⇒ 下一次 `POST /turns` 直接放行、
           `claimActiveRun` 在 `IS NULL` 上成功 ⇒ **同一会话两条状态机并行** ——
           正是本任务的互斥要防的事。

        白名单挡掉 `running`，但**故意**也挡掉「崩溃遗留」（进程被杀、状态卡在 `running`
        且已无活任务）：状态白名单**无法**区分「活着的 running」与「死掉的 running」，
        故该场景不由本端点负责，属于已推迟的 TTL / 扫描器 follow-up。

        终态 ⇒ 状态**不动**（取消 ≠ 删结果：把 `done` 翻成 aborted 会连报告一起作废），
        只补一次释放（终态闸门放行）⇒ 幂等无副作用。
        """
        result = await session.execute(
            update(ResearchSession)
            .where(
                ResearchSession.id == sessionId,
                ResearchSession.status == STATUS_AWAITING,
            )
            .values(status="aborted", active_run_id=None)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await self._syncSessionRow(session, sessionId, activeRunId=None, status="aborted")
            logger.info("会话已取消并释放执行权: id=%s", sessionId)
            return "aborted"
        status = await session.scalar(
            select(ResearchSession.status).where(ResearchSession.id == sessionId)
        )
        if status is None:
            logger.warning("取消请求落空：会话不存在: id=%s", sessionId)
            return None
        if status in TERMINAL_SESSION_STATUSES:
            await self.releaseActiveRun(session, sessionId, None)
            logger.debug("取消请求无副作用（会话已终态 %s）: id=%s", status, sessionId)
            return status
        # 既非 awaiting_user 又非终态 = 真在跑：状态与执行权**一律不碰**，交调用方拒绝
        logger.info("取消请求被拒（会话正在执行）: id=%s status=%s", sessionId, status)
        return status

    async def _setTerminalAndRelease(
        self, session: AsyncSession, sessionId: uuid.UUID, status: str
    ) -> None:
        """把会话推进到终态 `status` **并且**就地清空 `active_run_id`（一条语句）。

        为什么必须一条语句：分开写会留下中间态 —— 状态写成功而释放失败 ⇒ 锁死；
        状态写失败（释放被终态闸门挡住）⇒ 同样锁死。终态与释放同生共死，不留窗口。
        **不 commit** —— 事务归调用方（与其余方法一致）。
        """
        if status not in TERMINAL_SESSION_STATUSES:
            logger.warning("非法终态: id=%s status=%s", sessionId, status)
            raise ValueError(f"非法终态 {status}，合法值: {sorted(TERMINAL_SESSION_STATUSES)}")
        await session.execute(
            update(ResearchSession)
            .where(ResearchSession.id == sessionId)
            .values(status=status, active_run_id=None)
            .execution_options(synchronize_session=False)
        )
        await self._syncSessionRow(session, sessionId, activeRunId=None, status=status)
        logger.info("会话落终态并释放执行权: id=%s status=%s", sessionId, status)

    async def _syncSessionRow(
        self,
        session: AsyncSession,
        sessionId: uuid.UUID,
        *,
        activeRunId: uuid.UUID | None,
        status: str | None = None,
    ) -> None:
        """把**库中真实**的会话行值同步回 identity map 里的对象。

        认领 / 释放 / 落终态都走 UPDATE（`synchronize_session=False`）：同一次会话里
        先加载过该行的调用方（`runTurn` 的 `_loadSession`）对象属性会停在旧值。
        行已在 map 中时 `session.get` 不落 SQL，故本同步不增加往返。
        """
        row = await session.get(ResearchSession, sessionId)
        if row is None:
            return
        row.active_run_id = activeRunId
        if status is not None:
            row.status = status

    async def createRunState(
        self, session: AsyncSession, run_state: ResearchRunState
    ) -> ResearchRunState:
        """将已构建的 ResearchRunState 加入当前事务。

        调用方负责构造不可变对象（`research_run_state.buildRunState`）；
        本方法只执行 `session.add` 并原样返回，保证写入操作集中在服务层。
        """
        session.add(run_state)
        return run_state

    async def updateRunState(
        self, session: AsyncSession, run_state: ResearchRunState
    ) -> ResearchRunState:
        """将版本化后的新 ResearchRunState 加入当前事务。

        与 `createRunState` 对称：`research_run_state.updateRunState` 产出新对象后，
        由本方法统一落库，避免业务状态机直接操作 `session`。
        """
        session.add(run_state)
        return run_state

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
        enhancedClaim: str | None = None,  # NEW: non-empty → use as claim_text
        stats: dict[str, Any] | None = None,  # NEW: non-None → merge into supporting_data["stats"]
    ) -> ResearchFinding:
        """落一条结论（含支撑 SQL / 数据 / 置信度）。

        supportingData 边界 coerce（2026-10-09）：
        Oracle 19c 真机 rows 常含 DATE / TRUNC 列（``datetime``）和
        ``number(p,s)``（``Decimal``）。SQLAlchemy asyncpg 默认 JSON 编码器不认
        这两类，整条 INSERT 抛 ``TypeError: Object of type datetime is not JSON
        serializable``，导致 verify 阶段崩 → session failed → 前端看到"无提示 +
        报告不存在"。

        与项目 `json.dumps(..., default=str)` 既有约定一致（见 agent_tools_nl2sql
        / chat_service / report_template_service 等 5+ 处）：在写入边界把
        datetime/Decimal 转成 ISO 字符串 / float，**不**要求 caller 手工 coerce。
        """
        if not 0.0 <= confidence <= 1.0:
            logger.warning("置信度越界: session=%s confidence=%s", sessionId, confidence)
            raise ValueError(f"confidence 必须落在 [0, 1]: {confidence}")
        data = dict(supportingData or {})
        if stats is not None:
            data["stats"] = stats
        finalClaim = enhancedClaim if enhancedClaim else claimText
        coercedData = _coerceJsonSafe(data)
        row = ResearchFinding(
            session_id=sessionId,
            turn_id=turnId,
            claim_text=finalClaim,
            supporting_sql=supportingSql,
            supporting_data=coercedData,
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
            payload=_coerceJsonSafe(payload or {}),
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
