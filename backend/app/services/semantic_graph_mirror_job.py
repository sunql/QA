"""图谱镜像的**物化 Job**（P3b D23）。

**一句话**：把「一次已编译的语义发布」物化成图谱镜像的**编排层**——读发布快照 →
解析实体 → 建镜像计划 → 按批次幂等写图 → 记台账 → 收尾核对。

**为什么要有这一层**（D22 留下的三个洞）：

| 洞 | 后果 | 本层的处置 |
|---|---|---|
| L-5 没有状态标记 | 中途失败留下**半份镜像**，事后没人知道它半不半 | 作业状态 `materializing → materialized/failed` + 逐批台账 |
| L-7 已物化的快照可被就地覆盖 | 解析依赖实时 PG ⇒ 重跑可能得到**不同**结果 ⇒ 「发布 = 不可变快照」被悄悄破坏 | 默认拒写，必须显式 `--force-rebuild` |
| 失败无续跑 | 大批量物化失败 ⇒ 从头再来 | 批次**台账成功且图上计数完好**才跳过（**台账 = 意图，图 = 事实**），
    失败批从失败处继续 |

**边界声明**：

- 本模块是**编排**，不直接碰 PG / Neo4j ⇒ 所有 IO 走注入的 `MirrorJobPorts`
  （脚本 `scripts/semantic_graph_sync.py` 是唯一的装配点）；
- **fail-closed**：实体没解析全 ⇒ 默认**拒绝物化**（半份镜像比没镜像更危险：
  D25 读侧切过去会静默少实体），要带着缺口继续必须显式 `--allow-partial`；
- **DRAFT 不物化**：草稿还会变 ⇒ 物化出来的东西不是「某一版发布」。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.domain.enums import SemanticReleaseStatus
from app.domain.semantic_graph_mirror_spec import (
    MIRROR_JOB_BATCH_STATUS_FAILED,
    MIRROR_JOB_BATCH_STATUS_PLANNED,
    MIRROR_JOB_BATCH_STATUS_RUNNING,
    MIRROR_JOB_BATCH_STATUS_SKIPPED,
    MIRROR_JOB_BATCH_STATUS_SUCCEEDED,
    MIRROR_JOB_STATUS_FAILED,
    MIRROR_JOB_STATUS_MATERIALIZED,
    MIRROR_JOB_STATUS_MATERIALIZING,
    RELEASE_MIRROR_ENTITY_TYPES,
    RELEASE_MIRROR_REL_TYPES,
)
from app.services.semantic_graph_mirror import (
    EntityResolution,
    MirrorPlan,
    UnresolvedItem,
    buildMirrorPlan,
)

# 节点批次的批次号（按实体类型，一类一批 ⇒ 事务小、易续跑）
_NODE_BATCH_NOS: dict[str, str] = {
    "class": "B1",
    "property": "B2",
    "metric": "B3",
    "kpi": "B4",
}
# 边批次：按关系类型一批（端点类型由契约固定 ⇒ 同 relType 必同端点类型）
_EDGE_BATCH_NO_PREFIX = "B5"
# 裁剪批次（显式 opt-in）
_PRUNE_BATCH_NO = "B6"

# 可物化的发布状态：DRAFT 会被排除（草稿还会变）。
_MATERIALIZABLE_STATUSES: frozenset[str] = frozenset(
    {
        SemanticReleaseStatus.VALIDATED.value,
        SemanticReleaseStatus.ACTIVE.value,
        SemanticReleaseStatus.ARCHIVED.value,
    }
)


class MirrorJobBlocked(RuntimeError):
    """前置条件不满足（**不该**重试）：DRAFT 发布 / 已物化 / 实体没解析全。"""


class MirrorJobError(RuntimeError):
    """运行期失败（写图失败、收尾核对不一致等）—— 可以重试。"""


# =============================================================================
# 值对象
# =============================================================================


@dataclass(frozen=True)
class ReleaseSnapshot:
    """一次发布的**只读**快照（从 `semantic_release` 读出）。"""

    releaseId: int
    version: str
    status: str
    manifest: dict[str, Any]
    manifestHash: str


@dataclass(frozen=True)
class MirrorJobState:
    """作业级状态标记（「这一次发布物化到什么程度了」）。"""

    releaseId: int
    releaseVersion: str
    status: str
    manifestHash: str
    nodeCount: int = 0
    edgeCount: int = 0
    unresolvedCount: int = 0
    startedAt: str = ""
    updatedAt: str = ""
    finishedAt: str = ""
    error: str = ""


@dataclass(frozen=True)
class MirrorJobBatchState:
    """批次级台账（批次号 / 条数 / 耗时 / 失败原因）。"""

    releaseId: int
    batchNo: str
    kind: str
    status: str
    count: int = 0
    elapsedMs: int = 0
    error: str = ""
    manifestHash: str = ""
    updatedAt: str = ""


@dataclass(frozen=True)
class MirrorJobBatch:
    """一个待执行的批次（纯数据 ⇒ 可序列化、可复算）。"""

    batchNo: str
    kind: str  # nodes | edges | prune
    entityType: str = ""
    relType: str = ""
    rows: tuple[dict[str, Any], ...] = ()
    keepUnifiedIds: tuple[str, ...] = ()


@dataclass(frozen=True)
class MirrorJobResult:
    """一次物化的结果（脚本据此出报告与退出码）。"""

    releaseId: int
    releaseVersion: str
    status: str  # materialized | dry-run | failed
    dryRun: bool
    nodeCount: int = 0
    edgeCount: int = 0
    unresolvedCount: int = 0
    batches: tuple[MirrorJobBatchState, ...] = ()
    unresolved: tuple[UnresolvedItem, ...] = ()
    error: str = ""


# =============================================================================
# 端口（IO 全部走这里 ⇒ 单测用替身即可，不连 PG / Neo4j）
# =============================================================================


class MirrorJobPorts(Protocol):
    """物化 Job 需要的全部外部动作。"""

    async def loadRelease(
        self, *, releaseId: int | None, version: str | None
    ) -> ReleaseSnapshot: ...

    async def resolveEntities(
        self, manifest: Mapping[str, Any]
    ) -> EntityResolution: ...

    def writeNodes(
        self,
        entityType: str,
        releaseId: int,
        releaseVersion: str,
        rows: list[dict[str, Any]],
    ) -> int: ...

    def writeEdges(self, releaseId: int, edges: list[dict[str, Any]]) -> int: ...

    def prune(
        self, releaseId: int, keepUnifiedIds: set[str], *, confirm: bool
    ) -> int: ...

    def countStaleNodes(self, releaseId: int, keepUnifiedIds: set[str]) -> int:
        """统计「计划外」的镜像节点数（裁剪前的对账数）。"""
        ...

    def countNodes(self, releaseId: int) -> dict[str, int]: ...

    def countEdges(self, releaseId: int) -> dict[str, int]: ...

    def readState(self, releaseId: int) -> MirrorJobState | None: ...

    def writeState(self, state: MirrorJobState) -> None: ...

    def readBatches(self, releaseId: int) -> list[MirrorJobBatchState]: ...

    def writeBatch(self, batch: MirrorJobBatchState) -> None: ...


# =============================================================================
# 批次计划
# =============================================================================


def buildJobBatches(
    plan: MirrorPlan, *, includePrune: bool = False
) -> tuple[MirrorJobBatch, ...]:
    """把镜像计划拆成可执行批次。

    批次划分与 D22-B 一致，编号上做了两处细化（记录在 daily/D23.md）：

    - D22-B 的「B3 = Metric / Kpi 节点」拆成 B3 / B4（写入按实体类型分别调用，
      事务边界更清楚，续跑粒度更细）；
    - D22-B 的「B4 = 6 种边」编号成 `B5:<relType>`（每种关系类型一批），
      裁剪顺延为 B6。
    """
    batches: list[MirrorJobBatch] = []
    rowsByType = plan.nodeRowsByType()
    for entityType in RELEASE_MIRROR_ENTITY_TYPES:
        rows = rowsByType.get(entityType) or []
        batches.append(
            MirrorJobBatch(
                batchNo=_NODE_BATCH_NOS[entityType],
                kind="nodes",
                entityType=entityType,
                rows=tuple(rows),
            )
        )
    edgeRows = plan.edgeRows()
    for relType in sorted(RELEASE_MIRROR_REL_TYPES):
        rows = tuple(row for row in edgeRows if row["relType"] == relType)
        batches.append(
            MirrorJobBatch(
                batchNo=f"{_EDGE_BATCH_NO_PREFIX}:{relType}",
                kind="edges",
                relType=relType,
                rows=rows,
            )
        )
    if includePrune:
        keep = tuple(sorted({node.unifiedId for node in plan.nodes}))
        batches.append(
            MirrorJobBatch(
                batchNo=_PRUNE_BATCH_NO, kind="prune", keepUnifiedIds=keep
            )
        )
    return tuple(batches)


# =============================================================================
# 主流程
# =============================================================================


async def runMirrorJob(
    ports: MirrorJobPorts,
    *,
    releaseId: int | None = None,
    version: str | None = None,
    dryRun: bool = False,
    forceRebuild: bool = False,
    allowPartial: bool = False,
    prune: bool = False,
    pruneConfirm: bool = False,
    expectDelete: int | None = None,
    resume: bool = True,
) -> MirrorJobResult:
    """物化一次发布到图谱镜像。"""
    snapshot = await ports.loadRelease(releaseId=releaseId, version=version)
    _assertMaterializable(snapshot)
    prior = ports.readState(snapshot.releaseId)

    resolution = await ports.resolveEntities(snapshot.manifest)
    plan = buildMirrorPlan(
        snapshot.manifest,
        resolution,
        releaseId=snapshot.releaseId,
        releaseVersion=snapshot.version,
    )
    if plan.unresolved and not allowPartial:
        raise MirrorJobBlocked(
            f"有 {len(plan.unresolved)} 条未解析 ⇒ 镜像不完整，拒绝物化"
            f"（要带着缺口继续请显式加 --allow-partial）"
        )
    batches = buildJobBatches(plan, includePrune=prune)
    if dryRun:
        # dry-run 是**只读**的 ⇒ 「已物化」不构成拒绝理由。
        # 拒绝只该保护「写」（快照不可变），不该挡住「看」（运维要看到会怎么分批）。
        return _dryRunResult(snapshot, plan, batches)
    _assertNotMaterialized(prior, snapshot, forceRebuild=forceRebuild)
    if prune and not pruneConfirm:
        raise MirrorJobBlocked("裁剪会删除图数据，必须显式确认（--confirm-prune）")
    return await _execute(
        ports,
        snapshot,
        plan,
        batches,
        forceRebuild=forceRebuild,
        resume=resume,
        expectDelete=expectDelete,
    )


def _dryRunResult(
    snapshot: ReleaseSnapshot,
    plan: MirrorPlan,
    batches: Sequence[MirrorJobBatch],
) -> MirrorJobResult:
    """dry-run 只出计划：**一个字节都不写**（连状态标记都不写）。

    计划里带上**批次预览**（status=planned）⇒ 运维在真写前就能看到
    「会怎么分批、每批多少条」，而不是只看到一个总数。
    """
    return MirrorJobResult(
        releaseId=snapshot.releaseId,
        releaseVersion=snapshot.version,
        status="dry-run",
        dryRun=True,
        nodeCount=len(plan.nodes),
        edgeCount=len(plan.edges),
        unresolvedCount=len(plan.unresolved),
        batches=tuple(_plannedBatchState(snapshot.releaseId, b) for b in batches),
        unresolved=plan.unresolved,
    )


def _plannedBatchState(
    releaseId: int, batch: MirrorJobBatch
) -> MirrorJobBatchState:
    """把批次计划转成「预览态」台账行（status=planned，不落库）。"""
    return MirrorJobBatchState(
        releaseId=releaseId,
        batchNo=batch.batchNo,
        kind=batch.kind,
        status=MIRROR_JOB_BATCH_STATUS_PLANNED,
        count=len(batch.rows),
    )


async def _execute(
    ports: MirrorJobPorts,
    snapshot: ReleaseSnapshot,
    plan: MirrorPlan,
    batches: Sequence[MirrorJobBatch],
    *,
    forceRebuild: bool,
    resume: bool,
    expectDelete: int | None,
) -> MirrorJobResult:
    """执行批次并记账；中途失败 ⇒ 状态标记落 failed 并抛出。"""
    priorBatches = {batch.batchNo: batch for batch in ports.readBatches(snapshot.releaseId)}
    running = _runningState(snapshot, plan)
    ports.writeState(running)
    try:
        results, _ = await _runAllBatches(
            ports, snapshot, plan, batches, priorBatches,
            forceRebuild=forceRebuild, resume=resume, expectDelete=expectDelete,
        )
        nodeCount, edgeCount = _verifyCounts(ports, plan)
    except Exception as exc:
        ports.writeState(_failedState(running, str(exc)))
        raise
    finished = _materializedState(running, nodeCount=nodeCount, edgeCount=edgeCount)
    ports.writeState(finished)
    return MirrorJobResult(
        releaseId=snapshot.releaseId,
        releaseVersion=snapshot.version,
        status=MIRROR_JOB_STATUS_MATERIALIZED,
        dryRun=False,
        nodeCount=nodeCount,
        edgeCount=edgeCount,
        unresolvedCount=len(plan.unresolved),
        batches=tuple(results),
        unresolved=plan.unresolved,
    )


def _runningState(snapshot: ReleaseSnapshot, plan: MirrorPlan) -> MirrorJobState:
    startedAt = _now()
    return MirrorJobState(
        releaseId=snapshot.releaseId,
        releaseVersion=snapshot.version,
        status=MIRROR_JOB_STATUS_MATERIALIZING,
        manifestHash=snapshot.manifestHash,
        nodeCount=len(plan.nodes),
        edgeCount=len(plan.edges),
        unresolvedCount=len(plan.unresolved),
        startedAt=startedAt,
        updatedAt=startedAt,
    )


def _materializedState(
    running: MirrorJobState, *, nodeCount: int, edgeCount: int
) -> MirrorJobState:
    return MirrorJobState(
        releaseId=running.releaseId,
        releaseVersion=running.releaseVersion,
        status=MIRROR_JOB_STATUS_MATERIALIZED,
        manifestHash=running.manifestHash,
        nodeCount=nodeCount,
        edgeCount=edgeCount,
        unresolvedCount=running.unresolvedCount,
        startedAt=running.startedAt,
        updatedAt=_now(),
        finishedAt=_now(),
    )


async def _runAllBatches(
    ports: MirrorJobPorts,
    snapshot: ReleaseSnapshot,
    plan: MirrorPlan,
    batches: Sequence[MirrorJobBatch],
    priorBatches: Mapping[str, MirrorJobBatchState],
    *,
    forceRebuild: bool,
    resume: bool,
    expectDelete: int | None,
) -> tuple[list[MirrorJobBatchState], bool]:
    """跑完全部批次，返回 (批次状态列表, 本轮是否重跑了节点批次)。

    异常直接向上抛（调用方负责把作业状态落 failed）。
    """
    results: list[MirrorJobBatchState] = []
    # 依赖失效标记：本轮**真的重跑了节点批次** ⇒ 端点可能变了 ⇒ 边批次不许跳过
    # （否则「节点回来了、边没回来」⇒ 镜像半残，而台账还写着边批次已成功）
    nodeBatchesReplayed = False
    # 产出是否还在图上：台账只说「上轮做过什么」，**图上的计数才是「现在是什么」**
    intact = _OutputIntact.of(ports, snapshot.releaseId, plan)
    for batch in batches:
        result = _runBatch(
            ports,
            batch,
            snapshot,
            priorBatches,
            forceRebuild=forceRebuild,
            resume=resume,
            expectDelete=expectDelete,
            allowSkip=not (nodeBatchesReplayed and batch.kind != "nodes"),
            outputIntact=intact.isIntact(batch),
        )
        results.append(result)
        if batch.kind == "nodes" and result.status == MIRROR_JOB_BATCH_STATUS_SUCCEEDED:
            nodeBatchesReplayed = True
    return results, nodeBatchesReplayed


def _failedState(running: MirrorJobState, error: str) -> MirrorJobState:
    return MirrorJobState(
        releaseId=running.releaseId,
        releaseVersion=running.releaseVersion,
        status=MIRROR_JOB_STATUS_FAILED,
        manifestHash=running.manifestHash,
        nodeCount=running.nodeCount,
        edgeCount=running.edgeCount,
        unresolvedCount=running.unresolvedCount,
        startedAt=running.startedAt,
        updatedAt=_now(),
        error=error,
    )


# =============================================================================
# 单批次执行
# =============================================================================


def _batchState(
    batch: MirrorJobBatch,
    snapshot: ReleaseSnapshot,
    status: str,
    *,
    count: int = 0,
    elapsedMs: int = 0,
    error: str = "",
) -> MirrorJobBatchState:
    """构造一个批次台账状态（字段齐套，避免四处重复字面量）。"""
    return MirrorJobBatchState(
        releaseId=snapshot.releaseId,
        batchNo=batch.batchNo,
        kind=batch.kind,
        status=status,
        count=count,
        elapsedMs=elapsedMs,
        error=error,
        manifestHash=snapshot.manifestHash,
        updatedAt=_now(),
    )


def _runBatch(
    ports: MirrorJobPorts,
    batch: MirrorJobBatch,
    snapshot: ReleaseSnapshot,
    priorBatches: Mapping[str, MirrorJobBatchState],
    *,
    forceRebuild: bool,
    resume: bool,
    expectDelete: int | None,
    allowSkip: bool = True,
    outputIntact: bool = True,
) -> MirrorJobBatchState:
    """跑一个批次并记账；失败时把原因写进台账再抛出。

    `allowSkip=False` ⇒ 即使台账说「已成功」也必须重跑（依赖失效时用）。
    """
    prior = priorBatches.get(batch.batchNo)
    if allowSkip and _canSkip(
        prior,
        snapshot,
        forceRebuild=forceRebuild,
        resume=resume,
        outputIntact=outputIntact,
    ):
        assert prior is not None
        return _batchState(
            batch, snapshot, MIRROR_JOB_BATCH_STATUS_SKIPPED,
            count=prior.count, elapsedMs=prior.elapsedMs,
        )
    started = time.monotonic()
    ports.writeBatch(_batchState(batch, snapshot, MIRROR_JOB_BATCH_STATUS_RUNNING))
    try:
        count = _executeBatch(
            ports, batch, snapshot, expectDelete=expectDelete
        )
    except Exception as exc:
        ports.writeBatch(
            _batchState(batch, snapshot, MIRROR_JOB_BATCH_STATUS_FAILED, error=str(exc))
        )
        if isinstance(exc, MirrorJobBlocked):
            # 前置拒绝不是「运行失败」：类型要原样传上去（脚本据此给不同退出码）
            raise
        raise MirrorJobError(f"批次 {batch.batchNo} 失败：{exc}") from exc
    elapsedMs = int((time.monotonic() - started) * 1000)
    state = _batchState(
        batch, snapshot, MIRROR_JOB_BATCH_STATUS_SUCCEEDED,
        count=count, elapsedMs=elapsedMs,
    )
    ports.writeBatch(state)
    return state


def _canSkip(
    prior: MirrorJobBatchState | None,
    snapshot: ReleaseSnapshot,
    *,
    forceRebuild: bool,
    resume: bool,
    outputIntact: bool = True,
) -> bool:
    """这一批能不能跳过（续跑）。

    只有「同一份快照哈希 + 上次成功 + 不是强制重建 + **产出还在图上**」才能跳。

    `outputIntact` 是关键的一条：台账只说明「上轮做过什么」，**图上的计数才是
    「现在是什么」**。若图上这批的产出已经不在了（被人工删过 / 上轮只写了一半），
    照台账跳过 ⇒ 永远修不好（死锁）⇒ 必须重跑这一批。
    """
    if prior is None or not resume or forceRebuild:
        return False
    if not outputIntact:
        return False
    if prior.status != MIRROR_JOB_BATCH_STATUS_SUCCEEDED:
        return False
    return prior.manifestHash == snapshot.manifestHash


@dataclass(frozen=True)
class _OutputIntact:
    """判断「某批次的产出在图上是否还完整」（续跑跳过与否的**唯一事实来源**）。

    对账口径与收尾核对 `_verifyCounts` 完全一致（同一份计划、同一组计数）⇒
    「跳过时看到的数」与「跑完核对的数」不会是两套口径。
    """

    nodeCounts: Mapping[str, int]
    edgeCounts: Mapping[str, int]
    planNodeCounts: Mapping[str, int]
    planEdgeCounts: Mapping[str, int]

    @classmethod
    def of(
        cls, ports: MirrorJobPorts, releaseId: int, plan: MirrorPlan
    ) -> _OutputIntact:
        planNodeCounts = {
            entityType: len(rows) for entityType, rows in plan.nodeRowsByType().items()
        }
        planEdgeCounts: dict[str, int] = {}
        for row in plan.edgeRows():
            relType = row["relType"]
            planEdgeCounts[relType] = planEdgeCounts.get(relType, 0) + 1
        return cls(
            nodeCounts=dict(ports.countNodes(releaseId)),
            edgeCounts=dict(ports.countEdges(releaseId)),
            planNodeCounts=planNodeCounts,
            planEdgeCounts=planEdgeCounts,
        )

    def isIntact(self, batch: MirrorJobBatch) -> bool:
        """该批次的产出是否还在图上（计数与计划相等 ⇒ 完整）。"""
        if batch.kind == "nodes":
            entityType = batch.entityType
            return self.nodeCounts.get(entityType, 0) == self.planNodeCounts.get(
                entityType, 0
            )
        if batch.kind == "edges":
            relType = batch.relType
            return self.edgeCounts.get(relType, 0) == self.planEdgeCounts.get(
                relType, 0
            )
        # 裁剪是破坏性操作且用户显式点名了 ⇒ 每次都真跑（它自己会做数量对账）
        return False


def _executeBatch(
    ports: MirrorJobPorts,
    batch: MirrorJobBatch,
    snapshot: ReleaseSnapshot,
    *,
    expectDelete: int | None,
) -> int:
    if batch.kind == "nodes":
        return ports.writeNodes(
            batch.entityType,
            snapshot.releaseId,
            snapshot.version,
            list(batch.rows),
        )
    if batch.kind == "edges":
        return ports.writeEdges(snapshot.releaseId, list(batch.rows))
    return _pruneBatch(ports, batch, snapshot, expectDelete=expectDelete)


def _pruneBatch(
    ports: MirrorJobPorts,
    batch: MirrorJobBatch,
    snapshot: ReleaseSnapshot,
    *,
    expectDelete: int | None,
) -> int:
    """裁剪批次：**数量对账**通过才删（Neo4j 无备份 ⇒ 删了不可逆）。

    「不知道会删多少就别删」：没给 `--expect-delete` ⇒ 拒绝；给了但对不上 ⇒ 拒绝。
    """
    keep = set(batch.keepUnifiedIds)
    if expectDelete is None:
        raise MirrorJobBlocked(
            "裁剪必须显式给 --expect-delete <数量> 对账（不知道会删多少就别裁）"
        )
    stale = ports.countStaleNodes(snapshot.releaseId, keep)
    if stale != expectDelete:
        raise MirrorJobBlocked(
            f"裁剪将删 {stale} 个镜像节点，与 --expect-delete {expectDelete} "
            f"不一致 ⇒ 拒绝执行（数量对不上说明目标选错了）"
        )
    return ports.prune(snapshot.releaseId, keep, confirm=True)


# =============================================================================
# 收尾核对 + 前置守卫
# =============================================================================


def _verifyCounts(ports: MirrorJobPorts, plan: MirrorPlan) -> tuple[int, int]:
    """核对图上的计数与计划是否一致（不一致 ⇒ 判失败，不报「成功」）。

    节点/边数与计划不等 ⇒ 图上不是「这一版发布的镜像」（例如上一轮残留没清干净）。
    """
    nodeCounts = ports.countNodes(plan.releaseId)
    expectedNodes = {
        entityType: len(rows) for entityType, rows in plan.nodeRowsByType().items()
    }
    for entityType, expected in expectedNodes.items():
        actual = nodeCounts.get(entityType, 0)
        if actual != expected:
            raise MirrorJobError(
                f"镜像节点计数与计划不一致：{entityType} 期望 {expected}，"
                f"实际 {actual}（可能有上轮残留 ⇒ 带 --prune 重跑）"
            )
    edgeCounts = ports.countEdges(plan.releaseId)
    expectedEdges: dict[str, int] = {}
    for row in plan.edgeRows():
        expectedEdges[row["relType"]] = expectedEdges.get(row["relType"], 0) + 1
    for relType, expected in expectedEdges.items():
        actual = edgeCounts.get(relType, 0)
        if actual != expected:
            raise MirrorJobError(
                f"镜像边计数与计划不一致：{relType} 期望 {expected}，实际 {actual}"
                f"（可能有上轮残留 ⇒ 带 --prune 重跑）"
            )
    return sum(expectedNodes.values()), sum(expectedEdges.values())


def _assertMaterializable(snapshot: ReleaseSnapshot) -> None:
    """草稿不物化（草稿还会变 ⇒ 物化出来的不是「某一版发布」）。"""
    if snapshot.status == SemanticReleaseStatus.DRAFT.value:
        raise MirrorJobBlocked(
            f"发布 {snapshot.version} 仍是 DRAFT（草稿还会变）⇒ 不能物化成镜像"
        )
    if snapshot.status not in _MATERIALIZABLE_STATUSES:
        raise MirrorJobBlocked(
            f"发布 {snapshot.version} 的状态 {snapshot.status!r} 不可物化"
        )


def _assertNotMaterialized(
    prior: MirrorJobState | None,
    snapshot: ReleaseSnapshot,
    *,
    forceRebuild: bool,
) -> None:
    """已物化的发布默认拒写（L-7）。

    解析依赖**实时** PG ⇒ 重跑可能得到不同结果 ⇒ 就地覆盖会把「发布 = 不可变
    快照」悄悄破坏。要重建必须显式 `--force-rebuild`。
    """
    if prior is None or prior.status != MIRROR_JOB_STATUS_MATERIALIZED:
        return
    if forceRebuild:
        return
    if prior.manifestHash != snapshot.manifestHash:
        raise MirrorJobBlocked(
            f"发布 {snapshot.version} 已物化，但台账哈希 "
            f"{prior.manifestHash[:12]}… 与快照哈希 {snapshot.manifestHash[:12]}… "
            f"不一致 ⇒ 已物化的镜像与当前快照不是同一份"
            f"（要重建请显式加 --force-rebuild）"
        )
    raise MirrorJobBlocked(
        f"发布 {snapshot.version} 已物化（{prior.finishedAt or prior.updatedAt}）"
        f"⇒ 默认拒写（要重建请显式加 --force-rebuild）"
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
