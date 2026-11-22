"""发现运行的**受理与后台执行**（WP-ONT-AI A2 / G3-4）。

`POST /runs` 是异步入口：受理时只做**便宜的校验 + 建 RUNNING 行**，重活（连业务库
剖析 + 调 LLM 生成候选）放后台任务里跑。

## 会话隔离（本仓既有事故教训）

后台任务**不得借用请求级会话**：请求失败时 `getDb` 会 rollback，而 SQLAlchemy 的
rollback 会把 identity map 里所有实例标记 expired ⇒ 后台任务再读属性就触发同步懒加载
⇒ `MissingGreenlet`，还会把原始异常盖掉（2026-11-16 生产事故）。⇒ 后台一律
`ownSession()` 自开会话，并在**新会话里重读** run 行。

## 为什么表名走参数而不是落库

`tables`（要剖析的表子集）目前不设列：受理时把它闭包进后台任务即可。
这是**单进程 BackgroundTasks** 的已知边界（进程重启会丢任务）——G4 换成
持久化队列（`workflow_outbox` 同型）时，再把请求参数落到 run 行。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.discovery_pipeline_models import DiscoveryRun
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.infrastructure.database import ownSession
from app.services.discovery_candidate_service import DiscoveryCandidateService
from app.services.discovery_profiler import DiscoveryProfiler
from app.services.discovery_snapshot_service import MSG_SOURCE_DISABLED, STATUS_ACTIVE

logger = logging.getLogger(__name__)

MSG_SOURCE_NOT_FOUND = "发现源 {id} 不存在"
MSG_SNAPSHOT_NOT_FOUND = "发现源 {id} 还没有元数据快照，先采集快照再剖析"
MSG_SNAPSHOT_MISMATCH = "元数据快照 {snapshotId} 不属于发现源 {sourceId}"
MSG_RUN_ALREADY_RUNNING = "发现源 {id} 已有运行中的任务（run {runId}），请稍后再试"
MSG_RUN_NOT_FOUND = "发现运行 {runId} 不存在"


class DiscoveryRunService:
    """受理发现运行 + 后台执行（剖析 → 候选）。"""

    def __init__(
        self,
        *,
        profiler: DiscoveryProfiler | None = None,
        candidateService: DiscoveryCandidateService | None = None,
    ) -> None:
        self._profiler = profiler or DiscoveryProfiler()
        self._candidates = candidateService or DiscoveryCandidateService()

    async def startRun(
        self,
        session: AsyncSession,
        *,
        sourceId: int,
        snapshotId: int | None = None,
        createdBy: str | None = None,
        trigger: str = "MANUAL",
    ) -> DiscoveryRun:
        """受理一次运行：校验源/快照 → 建 RUNNING 行（**不执行**重活）。"""
        source = await session.get(DiscoverySource, sourceId)
        if source is None:
            raise NotFoundError(MSG_SOURCE_NOT_FOUND.format(id=sourceId))
        if source.status != STATUS_ACTIVE:
            raise ValidationError(MSG_SOURCE_DISABLED.format(id=sourceId))
        snapshot = await self._resolveSnapshot(session, sourceId, snapshotId)
        running = (
            await session.execute(
                select(DiscoveryRun)
                .where(
                    DiscoveryRun.source_id == sourceId,
                    DiscoveryRun.status == "RUNNING",
                )
                .limit(1)
            )
        ).scalars().first()
        if running is not None:
            raise ConflictError(
                MSG_RUN_ALREADY_RUNNING.format(id=sourceId, runId=running.id)
            )
        run = DiscoveryRun(
            source_id=sourceId,
            snapshot_id=snapshot.id,
            trigger=trigger,
            status="RUNNING",
            created_by=createdBy,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run

    async def runPipeline(
        self,
        runId: int,
        *,
        tables: Sequence[str] | None = None,
        withCandidates: bool = True,
        session: AsyncSession | None = None,
    ) -> None:
        """后台执行：剖析 → （可选）候选生成。**永不把异常抛给请求线程**。

        `session`：仅测试/脚本注入（仓库既有做法，见 `ReportPlanner.compose`）；
        生产**不传** ⇒ 走 `ownSession()` 自开会话（请求会话可能在 rollback 后 expired）。
        """
        if session is not None:
            await self._pipeline(session, runId, tables=tables, withCandidates=withCandidates)
            return
        async with ownSession() as own:
            await self._pipeline(
                own, runId, tables=tables, withCandidates=withCandidates
            )

    async def _pipeline(
        self,
        session: AsyncSession,
        runId: int,
        *,
        tables: Sequence[str] | None,
        withCandidates: bool,
    ) -> None:
        # 在新会话里**重读** run（请求会话的实例可能已 expired，见模块 docstring）。
        run = await session.get(DiscoveryRun, runId)
        if run is None:
            logger.error("发现运行 %s 不存在，后台任务放弃", runId)
            return
        sourceId, snapshotId = run.source_id, run.snapshot_id
        createdBy, trigger = run.created_by, run.trigger
        if snapshotId is None:
            logger.error("发现运行 %s 没有快照，后台任务放弃", runId)
            return
        try:
            await self._profiler.profileTables(
                session,
                sourceId=sourceId,
                snapshotId=snapshotId,
                tables=tables,
                createdBy=createdBy,
                trigger=trigger,
                runId=runId,
            )
        except Exception as exc:  # noqa: BLE001 - 剖析失败已由 profiler 落 FAILED
            logger.error("发现剖析失败（run=%s）: %s", runId, exc, exc_info=True)
            return
        if not withCandidates:
            return
        try:
            await self._candidates.generateCandidates(session, runId=runId)
        except Exception as exc:  # noqa: BLE001 - 候选失败不影响剖析结果
            # 无模型配置属**预期降级**（本机 / 未配 key），记 info；其余记 error。
            logger.log(
                logging.INFO
                if type(exc).__name__ == "NoAvailableModelError"
                else logging.ERROR,
                "候选生成未完成（run=%s，剖析结果保留）: %s",
                runId,
                exc,
            )

    async def getRun(self, session: AsyncSession, runId: int) -> DiscoveryRun:
        run = await session.get(DiscoveryRun, runId)
        if run is None:
            raise NotFoundError(MSG_RUN_NOT_FOUND.format(runId=runId))
        return run

    async def _resolveSnapshot(
        self, session: AsyncSession, sourceId: int, snapshotId: int | None
    ) -> MetadataSnapshot:
        if snapshotId is None:
            latest = (
                await session.execute(
                    select(MetadataSnapshot)
                    .where(MetadataSnapshot.source_id == sourceId)
                    .order_by(
                        MetadataSnapshot.captured_at.desc(),
                        MetadataSnapshot.id.desc(),
                    )
                    .limit(1)
                )
            ).scalars().first()
            if latest is None:
                raise ValidationError(MSG_SNAPSHOT_NOT_FOUND.format(id=sourceId))
            return latest
        snapshot = await session.get(MetadataSnapshot, snapshotId)
        if snapshot is None:
            raise NotFoundError(
                MSG_SNAPSHOT_NOT_FOUND.format(id=sourceId)
            )
        if snapshot.source_id != sourceId:
            raise ValidationError(
                MSG_SNAPSHOT_MISMATCH.format(snapshotId=snapshotId, sourceId=sourceId)
            )
        return snapshot
