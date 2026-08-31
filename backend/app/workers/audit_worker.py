"""审计 Outbox worker（feat-audit-outbox，Phase 4.5 扩展）。

独立进程消费 audit_outbox：写 audit_log（幂等键 outbox_id）+ 按事件路由写
kpi_catalog_history / feature_definition_history 快照。审计失败不回滚业务
（业务事务只入队）；本进程失败则 outbox.attempts 递增，达上限后放弃该行
（毒丸防护）并告警日志。

启动：uv run python -m app.workers.audit_worker
优雅停机：SIGTERM / SIGINT -> 处理完当前批次退出。

事务边界（每条消息独立事务，与业务进程完全解耦）：
    开 session -> 逐条 SELECT 单行（消费幂等靠 audit_log.outbox_id 唯一索引
    + processed_at 更新的条件 UPDATE 兜底）-> commit
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import (
    AuditLog,
    AuditOutbox,
    FeatureDefinition,
    FeatureDefinitionHistory,
    KpiCatalog,
    KpiCatalogHistory,
)
from app.services.audit_service import AuditService
from app.services.history_service import HistoryService

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 1.0
_DEFAULT_BATCH_SIZE = 100
_DEFAULT_MAX_ATTEMPTS = 5

# event_type 动词 -> audit_log action（AuditService._VALID_ACTIONS）
_ACTION_BY_VERB = {"created": "CREATE", "updated": "UPDATE", "deleted": "DELETE"}

# entity_type -> 业务实体表（历史快照的存在性守卫：实体已删则跳过 history）
_ENTITY_TABLE_BY_TYPE: dict[str, Any] = {
    "kpi_catalog": KpiCatalog,
    "feature_definition": FeatureDefinition,
}


class AuditWorker:
    """轮询 audit_outbox 并落地审计写入的消费者。"""

    def __init__(
        self,
        pollInterval: float = _DEFAULT_POLL_INTERVAL,
        batchSize: int = _DEFAULT_BATCH_SIZE,
        maxAttempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._pollInterval = pollInterval
        self._batchSize = batchSize
        self._maxAttempts = maxAttempts
        self._stopEvent = asyncio.Event()
        self._audit = AuditService()
        self._history = HistoryService()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def requestStop(self) -> None:
        """请求优雅停机（处理完当前批次后退出 run 循环）。"""
        self._stopEvent.set()

    async def run(self) -> None:
        """主循环：轮询 drain，直到收到停止信号。"""
        from app.infrastructure.database import disposeEngine, getSessionFactory

        factory = getSessionFactory()
        registerSignalHandlers(self.requestStop)
        try:
            while not self._stopEvent.is_set():
                try:
                    async with factory() as session:
                        await self.drainOnce(session)
                except Exception:
                    # DB 不可达等基础设施故障：记日志继续轮询（退避交给上层
                    # 重启策略 / 容器编排）
                    logger.exception("outbox drain 轮次失败，下轮重试")
                # 允许在 sleep 中被停机信号打断
                try:
                    await asyncio.wait_for(
                        self._stopEvent.wait(), timeout=self._pollInterval
                    )
                except TimeoutError:
                    pass
        finally:
            await disposeEngine()
            logger.info("audit worker stopped")

    # ------------------------------------------------------------------
    # 消费
    # ------------------------------------------------------------------

    async def drainOnce(self, session: AsyncSession) -> int:
        """处理一批 pending 消息，返回成功条数（失败/跳过不计）。

        每条消息独立提交：单条失败不影响同批其他消息（attempts 递增后
        继续），批次内无跨消息事务。
        """
        stmt = (
            select(AuditOutbox)
            .where(
                AuditOutbox.processed_at.is_(None),
                AuditOutbox.attempts < self._maxAttempts,
            )
            .order_by(AuditOutbox.created_at)
            .limit(self._batchSize)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return 0

        ok = 0
        for row in rows:
            if await self._processOne(session, row):
                ok += 1
        return ok

    async def _processOne(self, session: AsyncSession, row: AuditOutbox) -> bool:
        """处理单条消息。成功 -> processed_at 置 now；失败 -> attempts+1 + last_error。

        row 的属性在事务开始前捕获为本地值（rollback/commit 会使 ORM 对象
        过期，事后访问触发同步惰性加载 -> MissingGreenlet）。
        """
        rowId = row.id
        entityType = row.entity_type
        entityId = row.entity_id
        attemptsBefore = row.attempts
        try:
            await self._applyEvent(session, row)
            await session.commit()
            # 消费幂等兜底：条件 UPDATE，另一 worker 已处理则本侧 no-op
            result = await session.execute(
                AuditOutbox.__table__.update()
                .where(AuditOutbox.id == rowId, AuditOutbox.processed_at.is_(None))
                .values(processed_at=datetime.now(timezone.utc))
            )
            await session.commit()
            if result.rowcount == 1:
                logger.info(
                    "outbox %s processed（%s/%s）", rowId, entityType, entityId
                )
                return True
            return False  # 已被并发 worker 处理
        except Exception as exc:
            await session.rollback()
            # 截断避免泄露内部路径/堆栈细节（last_error 对 operator API 可见）
            err_msg = f"{type(exc).__name__}: {exc}"
            err = err_msg[:500] if len(err_msg) > 500 else err_msg
            await session.execute(
                AuditOutbox.__table__.update()
                .where(AuditOutbox.id == rowId)
                .values(
                    attempts=AuditOutbox.attempts + 1,
                    last_error=err,
                )
            )
            await session.commit()
            if attemptsBefore + 1 >= self._maxAttempts:
                logger.error(
                    "outbox %s 达到最大重试次数（%s），放弃并待人工处理：%s",
                    rowId, self._maxAttempts, err,
                )
            else:
                logger.warning(
                    "outbox %s 处理失败（attempts=%s）：%s",
                    rowId, attemptsBefore + 1, err,
                )
            return False

    async def _applyEvent(self, session: AsyncSession, row: AuditOutbox) -> None:
        """把一条 outbox 事件写入 audit_log（+ 历史快照，若有路由）。"""
        payload = row.payload or {}
        before = payload.get("before")
        after = payload.get("after")
        verb = row.event_type.rsplit("_", 1)[1]
        action = _ACTION_BY_VERB[verb]

        # 幂等前置检查：该 outbox 已写 audit_log 则跳过写入（唯一索引兜底）
        existing = await session.execute(
            select(AuditLog.id).where(AuditLog.outbox_id == row.id)
        )
        if existing.scalar_one_or_none() is not None:
            return

        await self._audit.record(
            session,
            entity_type=row.entity_type,
            entity_id=row.entity_id or 0,
            action=action,
            actor=row.actor,
            actor_departments=tuple(row.actor_departments or ()),
            before=before,
            after=after,
            outbox_id=row.id,
        )
        await session.flush()

        # 历史快照路由（KPI / FeatureDefinition 的 create/update）
        if row.entity_type in _ENTITY_TABLE_BY_TYPE and action in ("CREATE", "UPDATE") and after:
            await self._writeHistorySnapshot(session, row, after)

    async def _writeHistorySnapshot(
        self,
        session: AsyncSession,
        row: AuditOutbox,
        after: dict[str, Any],
    ) -> None:
        """按实体类型写历史快照行（payload.after 即完整快照）。

        实体存在性守卫：outbox 是延迟消费，若实体在消费前已被删除
        （create+delete 事件同批积压），历史行 FK 无法满足 -> 跳过 history
        写入（audit_log 已捕获变更事实，history 尽力而为）。
        """
        entityCls = _ENTITY_TABLE_BY_TYPE[row.entity_type]
        exists = await session.get(entityCls, row.entity_id)
        if exists is None:
            logger.info(
                "outbox %s：实体 %s/%s 已不存在，跳过历史快照（audit_log 仍记录）",
                row.id, row.entity_type, row.entity_id,
            )
            return
        if entityCls is KpiCatalog:
            snapshotRow = KpiCatalogHistory(
                kpi_id=row.entity_id,
                revision=int(after.get("revision_count", 0) or 0),
                snapshot_json=after,
                changed_by=row.actor,
            )
        elif entityCls is FeatureDefinition:
            snapshotRow = FeatureDefinitionHistory(
                feature_id=row.entity_id,
                snapshot_json=after,
                changed_by=row.actor,
            )
        else:  # pragma: no cover - 路由表扩展时兜底
            logger.warning("未知历史实体路由：%s", entityCls)
            return
        session.add(snapshotRow)
        await session.flush()


def registerSignalHandlers(requestStop: Any) -> None:
    """注册 SIGTERM/SIGINT -> 优雅停机（仅主线程可用；测试里不调用）。"""

    def _handler(signum, frame) -> None:
        logging.getLogger(__name__).info("收到信号 %s，准备停机", signum)
        requestStop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # 非主线程（测试并发场景）：跳过注册
            pass


async def main() -> None:
    """进程入口：python -m app.workers.audit_worker"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = AuditWorker()
    logger.info("audit worker 启动（poll=%ss batch=%s max_attempts=%s）",
                worker._pollInterval, worker._batchSize, worker._maxAttempts)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
