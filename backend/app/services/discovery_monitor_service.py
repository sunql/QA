"""发现域**增量监测**（WP-ONT-AI A3 / G4-1）。

一次监测 = 「采集最新快照 → 与上一次比」：

```
captureSnapshot（内容寻址、幂等）
  ├─ reused=true（内容没变）⇒ changed=false，**不起运行**（不烧 LLM）
  └─ reused=false（内容变了）⇒ 建一个 RUNNING 运行（剖析 + 候选交给后台）
```

## 为什么「变没变」不用自己 diff

`metadata_snapshot` 本身就是内容寻址的（`schema_hash`）：采集返回 `reused=true`
就等价于「同一份结构已经见过」。自己再按表/列 diff 一遍是第二套口径，
且与快照哈希有漂移风险。

## 为什么监测不自动生成提案

`discovery_candidate` 是**候选**（D-3：候选不是结论）。自动把候选拼成 manifest
等于机器替人拍板本体变更 —— 触碰「AI 写本体」红线。监测只负责「告诉你变了 + 产出
候选/问题」，提案仍走 G3-3 的人工组装 + 四眼审核。

## 周期驱动

本模块只提供**一次检查**；周期由外部（cron / 调度器）调 `checkSource`。仓库现有
`agent_schedule` / workflow worker 都可以接，但本批不新增调度进程（P5 裁决：
仓内零队列库 ⇒ 不自造 Worker 拓扑）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.discovery_models import DiscoverySource
from app.domain.discovery_pipeline_models import DiscoveryRun
from app.domain.exceptions import NotFoundError, ValidationError
from app.services.discovery_run_service import DiscoveryRunService
from app.services.discovery_snapshot_service import (
    STATUS_ACTIVE,
    DiscoverySnapshotService,
)

logger = logging.getLogger(__name__)

MSG_SOURCE_NOT_FOUND = "发现源 {id} 不存在"
MSG_SOURCE_DISABLED = "发现源已停用，不允许监测：{id}"


@dataclass(frozen=True, slots=True)
class MonitorResult:
    """一次监测的结论。"""

    sourceId: int
    snapshotId: int
    changed: bool
    runId: int | None


class DiscoveryMonitorService:
    """增量监测（采集 → 判变 → 起运行）。"""

    def __init__(
        self,
        *,
        snapshotService: DiscoverySnapshotService | None = None,
        runService: DiscoveryRunService | None = None,
    ) -> None:
        self._snapshots = snapshotService or DiscoverySnapshotService()
        self._runs = runService or DiscoveryRunService()

    async def checkSource(
        self,
        session: AsyncSession,
        *,
        sourceId: int,
        createdBy: str | None = None,
    ) -> MonitorResult:
        """采集一次并判断是否变化；变了就建一个 RUNNING 运行（不执行）。"""
        source = await session.get(DiscoverySource, sourceId)
        if source is None:
            raise NotFoundError(MSG_SOURCE_NOT_FOUND.format(id=sourceId))
        if source.status != STATUS_ACTIVE:
            raise ValidationError(MSG_SOURCE_DISABLED.format(id=sourceId))

        capture = await self._snapshots.captureSnapshot(
            session, sourceId=sourceId, createdBy=createdBy
        )
        if capture.reused:
            # 内容没变：不烧剖析/LLM。顺带把历史上卡在 RUNNING 的运行留给超时清理。
            return MonitorResult(
                sourceId=sourceId, snapshotId=capture.snapshotId, changed=False, runId=None
            )
        run: DiscoveryRun = await self._runs.startRun(
            session,
            sourceId=sourceId,
            snapshotId=capture.snapshotId,
            createdBy=createdBy,
            trigger="DIFF",
        )
        logger.info(
            "发现源 %s 结构变化（snapshot=%s）⇒ 起运行 %s",
            sourceId,
            capture.snapshotId,
            run.id,
        )
        return MonitorResult(
            sourceId=sourceId,
            snapshotId=capture.snapshotId,
            changed=True,
            runId=run.id,
        )
