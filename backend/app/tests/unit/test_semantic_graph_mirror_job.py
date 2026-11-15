"""图谱镜像物化 Job 的单测（P3b D23）。

Job 是**编排层**：读发布快照 → 解析 → 建计划 → 按批次幂等写图 → 记账 → 校验。
本文件用**替身端口**（`MirrorJobPorts`）驱动，不连 Neo4j / PG。

要测的行为（每条都对应一个真实事故模式）：

- **dry-run 不写任何东西**（连状态标记都不写）；
- **DRAFT 发布拒物化**（草稿还会变 ⇒ 镜像不是发布快照）；
- **已物化的发布默认拒写**（L-7：解析依赖实时 PG，重跑可能得到不同结果 ⇒
  快照被就地覆盖就毁了「发布 = 不可变快照」的前提）；
- **实体没解析全 ⇒ 默认拒物化**（fail-closed：半份镜像比没镜像更危险，
  D25 读侧切过去会静默少实体）；
- **分批 + 记账**（每批有条数与耗时，失败批记原因）；
- **失败可续跑**（成功过的批次不重写，失败批从失败处继续）；
- **写完要核对**（图上计数与计划不一致 ⇒ 判失败，不报「成功」）。

设计依据：`Harness/changes/feat-semantic-release-graph-mirror/daily/D23.md`。
"""

from __future__ import annotations

import pytest

from app.services.semantic_graph_mirror import EntityResolution
from app.services.semantic_graph_mirror_job import (
    MirrorJobBlocked,
    MirrorJobError,
    MirrorJobState,
    buildJobBatches,
    runMirrorJob,
)

# =============================================================================
# 夹具
# =============================================================================


def _manifest() -> dict:
    return {
        "classes": [
            {
                "className": "采购订单",
                "classAlias": "PO",
                "description": "采购订单",
                "sourceTable": "po",
                "parentClassName": None,
                "properties": [
                    {
                        "propertyName": "订单号",
                        "propertyAlias": "单号",
                        "dataType": "STRING",
                        "sourceColumn": "po_no",
                        "isPrimaryKey": True,
                        "isForeignKey": False,
                    }
                ],
            },
            {
                "className": "供应商",
                "classAlias": None,
                "description": None,
                "sourceTable": None,
                "parentClassName": None,
                "properties": [],
            },
        ],
        "joins": [
            {
                "joinKey": "po_supplier",
                "sourceClassName": "采购订单",
                "targetClassName": "供应商",
            }
        ],
        "metrics": [],
        "kpis": [],
    }


def _resolution() -> EntityResolution:
    return EntityResolution(
        classIds={"采购订单": "obj:CLASS:1", "供应商": "obj:CLASS:2"},
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={},
        kpiIds={},
    )


class _FakePorts:
    """`MirrorJobPorts` 的替身：记录所有写动作，可在指定批次上抛错。"""

    def __init__(self, *, resolution: EntityResolution | None = None) -> None:
        self.resolution = resolution if resolution is not None else _resolution()
        self.snapshot = None
        self.nodeWrites: list[tuple[str, int]] = []
        self.edgeWrites: list[int] = []
        self.prunes: list[set[str]] = []
        self.states: list[MirrorJobState] = []
        self.batchWrites: list[tuple[str, str]] = []
        self.failOn: str = ""
        self.nodeCounts: dict[str, int] = {}
        self.edgeCounts: dict[str, int] = {}
        self._edgeCountsByType: dict[str, int] = {}
        self.staleCount: int = 0

    # --- 发布快照 / 解析 ---------------------------------------------------
    async def loadRelease(self, *, releaseId, version):
        return self.snapshot

    async def resolveEntities(self, manifest):
        return self.resolution

    # --- 图写 -------------------------------------------------------------
    def writeNodes(self, entityType, releaseId, releaseVersion, rows):
        if self.failOn == f"nodes:{entityType}":
            raise RuntimeError(f"写入 {entityType} 失败")
        self.nodeWrites.append((entityType, len(rows)))
        return len(rows)

    def writeEdges(self, releaseId, edges):
        if self.failOn == "edges":
            raise RuntimeError("写入边失败")
        self.edgeWrites.append(len(edges))
        for edge in edges:
            relType = edge["relType"]
            self._edgeCountsByType[relType] = (
                self._edgeCountsByType.get(relType, 0) + 1
            )
        return len(edges)

    def prune(self, releaseId, keepUnifiedIds, *, confirm):
        if not confirm:
            raise ValueError("prune 必须显式确认")
        self.prunes.append(set(keepUnifiedIds))
        return 0

    def countStaleNodes(self, releaseId, keepUnifiedIds):
        return self.staleCount

    def countNodes(self, releaseId):
        if self.nodeCounts:
            return dict(self.nodeCounts)
        counts: dict[str, int] = {"class": 0, "property": 0, "metric": 0, "kpi": 0}
        for entityType, count in self.nodeWrites:
            counts[entityType] = counts.get(entityType, 0) + count
        return counts

    def countEdges(self, releaseId):
        if self.edgeCounts:
            return dict(self.edgeCounts)
        return dict(self._edgeCountsByType)

    # --- 台账 -------------------------------------------------------------
    def readState(self, releaseId):
        return self.states[-1] if self.states else None

    def writeState(self, state):
        self.states.append(state)

    def readBatches(self, releaseId):
        return []

    def writeBatch(self, batch):
        self.batchWrites.append((batch.batchNo, batch.status))


def _ports(**kwargs) -> _FakePorts:
    ports = _FakePorts(**kwargs)
    ports.snapshot = _snapshot()
    return ports


def _snapshot(*, status: str = "ACTIVE", manifestHash: str = "a" * 64):
    from app.services.semantic_graph_mirror_job import ReleaseSnapshot

    return ReleaseSnapshot(
        releaseId=7,
        version="2026.11.09-1",
        status=status,
        manifest=_manifest(),
        manifestHash=manifestHash,
    )


def _run(ports: _FakePorts, **kwargs):
    import asyncio

    return asyncio.run(runMirrorJob(ports, releaseId=7, **kwargs))


# =============================================================================
# 批次计划
# =============================================================================


def test_批次按实体类型与关系类型拆分() -> None:
    from app.services.semantic_graph_mirror import buildMirrorPlan

    plan = buildMirrorPlan(
        _manifest(), _resolution(), releaseId=7, releaseVersion="v1.2.0"
    )
    batches = buildJobBatches(plan)
    batchNos = [batch.batchNo for batch in batches]
    # 节点按 label 一批、边按 relType 一批
    assert "B1" in batchNos and "B2" in batchNos
    assert any(no.startswith("B5:") for no in batchNos)
    # 裁剪是显式 opt-in ⇒ 默认批次里没有
    assert not any(batch.kind == "prune" for batch in batches)


def test_裁剪批次显式开启才出现() -> None:
    from app.services.semantic_graph_mirror import buildMirrorPlan

    plan = buildMirrorPlan(
        _manifest(), _resolution(), releaseId=7, releaseVersion="v1.2.0"
    )
    batches = buildJobBatches(plan, includePrune=True)
    pruneBatches = [batch for batch in batches if batch.kind == "prune"]
    assert len(pruneBatches) == 1
    # 裁剪要带上「保留清单」= 计划内所有 unified_id
    assert "obj:CLASS:1" in pruneBatches[0].keepUnifiedIds


# =============================================================================
# 前置守卫
# =============================================================================


def test_dry_run_不写任何东西() -> None:
    ports = _ports()
    result = _run(ports, dryRun=True)
    assert result.status == "dry-run"
    assert ports.nodeWrites == []
    assert ports.edgeWrites == []
    assert ports.states == []  # 连状态标记都不写
    assert ports.batchWrites == []


def test_dry_run_给出计划批次供预览() -> None:
    """dry-run 只出计划 ⇒ 但计划里应当看得见**会怎么分批**（运维要预览范围）。"""
    ports = _ports()
    result = _run(ports, dryRun=True)
    assert result.batches, "dry-run 必须给出批次计划"
    assert all(batch.status == "planned" for batch in result.batches)
    nodeTotal = sum(b.count for b in result.batches if b.kind == "nodes")
    edgeTotal = sum(b.count for b in result.batches if b.kind == "edges")
    assert nodeTotal == result.nodeCount
    assert edgeTotal == result.edgeCount
    assert not ports.batchWrites, "计划只是预览，不许写台账"


def test_draft发布拒绝物化() -> None:
    ports = _ports()
    ports.snapshot = _snapshot(status="DRAFT")
    with pytest.raises(MirrorJobBlocked, match="DRAFT"):
        _run(ports)
    assert ports.nodeWrites == []


def test_已物化的发布默认拒写() -> None:
    ports = _ports()
    ports.states.append(
        MirrorJobState(
            releaseId=7,
            releaseVersion="2026.11.09-1",
            status="materialized",
            manifestHash="a" * 64,
        )
    )
    with pytest.raises(MirrorJobBlocked, match="已物化"):
        _run(ports)
    assert ports.nodeWrites == []


def test_dry_run_对已物化的发布也放行() -> None:
    """dry-run 是**只读**的 ⇒ 「已物化」不构成拒绝理由。

    真实踩到：发布已物化后，运维想再看看「会怎么分批」⇒ dry-run 也被拒 ⇒
    拿不到任何信息（拒绝只该保护「写」，不该挡住「看」）。
    """
    ports = _ports()
    ports.states.append(
        MirrorJobState(
            releaseId=7,
            releaseVersion="2026.11.09-1",
            status="materialized",
            manifestHash="a" * 64,
        )
    )
    result = _run(ports, dryRun=True)
    assert result.status == "dry-run"
    assert result.batches
    assert ports.nodeWrites == []


def test_force_rebuild_允许重写已物化的发布() -> None:
    ports = _ports()
    ports.states.append(
        MirrorJobState(
            releaseId=7,
            releaseVersion="2026.11.09-1",
            status="materialized",
            manifestHash="a" * 64,
        )
    )
    result = _run(ports, forceRebuild=True)
    assert result.status == "materialized"
    assert ports.nodeWrites


def test_清单哈希不一致时也要_force_rebuild() -> None:
    ports = _ports()
    ports.states.append(
        MirrorJobState(
            releaseId=7,
            releaseVersion="2026.11.09-1",
            status="materialized",
            manifestHash="b" * 64,
        )
    )
    with pytest.raises(MirrorJobBlocked, match="哈希"):
        _run(ports)


def test_实体没解析全时默认拒绝物化() -> None:
    """半份镜像比没镜像更危险：D25 读侧切过去会静默少实体。"""
    ports = _ports()
    ports.resolution = EntityResolution(
        classIds={"采购订单": "obj:CLASS:1"},  # 「供应商」没解析到
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={},
        kpiIds={},
    )
    with pytest.raises(MirrorJobBlocked, match="未解析"):
        _run(ports)
    assert ports.nodeWrites == []


def test_allow_partial_允许带着未解析继续物化() -> None:
    ports = _ports()
    ports.resolution = EntityResolution(
        classIds={"采购订单": "obj:CLASS:1"},
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={},
        kpiIds={},
    )
    result = _run(ports, allowPartial=True)
    assert result.status == "materialized"
    assert result.unresolvedCount >= 1


# =============================================================================
# 分批执行与记账
# =============================================================================


def test_每批记录状态与条数() -> None:
    ports = _ports()
    result = _run(ports)
    statuses = {batch.batchNo: batch.status for batch in result.batches}
    assert all(status == "succeeded" for status in statuses.values())
    counts = {batch.batchNo: batch.count for batch in result.batches}
    assert counts["B1"] == 2  # 两个类
    assert counts["B2"] == 1  # 一个属性
    assert counts["B5:JOIN"] == 1


def test_每批记录耗时且不为负() -> None:
    ports = _ports()
    result = _run(ports)
    assert all(batch.elapsedMs >= 0 for batch in result.batches)


def test_批次失败时状态标记记失败并带原因() -> None:
    ports = _ports()
    ports.failOn = "nodes:property"
    with pytest.raises(MirrorJobError):
        _run(ports)
    finalState = ports.states[-1]
    assert finalState.status == "failed"
    assert "写入 property 失败" in finalState.error


def test_失败后可续跑_成功过的批次不重写() -> None:
    ports = _ports()
    ports.failOn = "nodes:property"
    with pytest.raises(MirrorJobError):
        _run(ports)

    # 第二轮：失败原因消失，且把「B1 已成功」记进台账
    ports.failOn = ""
    writtenBatches: dict[str, str] = {}
    originalWriteBatch = ports.writeBatch

    def _writeBatch(batch):  # noqa: N802 - 替身方法名
        originalWriteBatch(batch)
        writtenBatches[batch.batchNo] = batch.status

    ports.writeBatch = _writeBatch  # type: ignore[method-assign]
    ports.readBatches = lambda releaseId: _priorBatches(ports)  # type: ignore[method-assign]

    result = _run(ports, resume=True)
    assert result.status == "materialized"
    # B1 续跑时是「跳过」，B2 重写
    statuses = {batch.batchNo: batch.status for batch in result.batches}
    assert statuses["B1"] == "skipped"
    assert statuses["B2"] == "succeeded"


def _priorBatches(ports: _FakePorts):
    """把第一轮的成功批次整理成「续跑时读到的台账」。"""
    from app.services.semantic_graph_mirror_job import MirrorJobBatchState

    seen: dict[str, MirrorJobBatchState] = {}
    for batchNo, status in ports.batchWrites:
        if status == "succeeded":
            seen[batchNo] = MirrorJobBatchState(
                releaseId=7,
                batchNo=batchNo,
                kind="nodes",
                status="succeeded",
                count=1,
                elapsedMs=1,
                manifestHash="a" * 64,
            )
    return list(seen.values())


def test_台账说成功但图上缺东西时不许跳过() -> None:
    """台账是「上轮做过什么」，**图才是「现在是什么」** ⇒ 跳过与否看图。

    真实事故：镜像节点被人工删掉（连带边也没了），台账仍写着批次成功
    ⇒ 按台账跳过 ⇒ 永远修不好（死锁）。
    """
    from app.services.semantic_graph_mirror_job import MirrorJobBatchState

    ports = _ports()
    # 图上：节点都在，但边没了（人工删节点 ⇒ 边也没了）
    ports.nodeWrites.extend([("class", 2), ("property", 1)])
    ports.readBatches = lambda releaseId: [  # noqa: ARG005 - 替身签名
        MirrorJobBatchState(
            releaseId=7,
            batchNo="B1",
            kind="nodes",
            status="succeeded",
            count=2,
            manifestHash="a" * 64,
        ),
        MirrorJobBatchState(
            releaseId=7,
            batchNo="B2",
            kind="nodes",
            status="succeeded",
            count=1,
            manifestHash="a" * 64,
        ),
        MirrorJobBatchState(
            releaseId=7,
            batchNo="B3",
            kind="nodes",
            status="succeeded",
            count=0,
            manifestHash="a" * 64,
        ),
        MirrorJobBatchState(
            releaseId=7,
            batchNo="B4",
            kind="nodes",
            status="succeeded",
            count=0,
            manifestHash="a" * 64,
        ),
        MirrorJobBatchState(
            releaseId=7,
            batchNo="B5:HAS_PROPERTY",
            kind="edges",
            status="succeeded",
            count=1,
            manifestHash="a" * 64,
        ),
    ]
    result = _run(ports, resume=True)
    statuses = {batch.batchNo: batch.status for batch in result.batches}
    assert statuses["B5:HAS_PROPERTY"] == "succeeded", (
        "图上边数对不上 ⇒ 该批次不许跳过（台账说成功不算数）"
    )
    assert result.status == "materialized"


def test_节点批次重跑时边批次不许跳过() -> None:
    """依赖失效：节点批次重跑 ⇒ 边批次必须跟着重跑。

    真实事故：镜像节点被人工删掉后，只重跑了节点批次 ⇒ 节点回来了、边没回来
    ⇒ 镜像半残（而「边批次已成功」的台账会让人以为不用重跑）。
    """
    from app.services.semantic_graph_mirror_job import MirrorJobBatchState

    ports = _ports()
    ports.nodeWrites.append(("class", 2))  # 上一轮 B1 已写（续跑时会被跳过）
    ports.readBatches = lambda releaseId: [  # noqa: ARG005 - 替身签名
        MirrorJobBatchState(
            releaseId=7,
            batchNo="B1",
            kind="nodes",
            status="succeeded",
            count=2,
            manifestHash="a" * 64,
        ),
        MirrorJobBatchState(
            releaseId=7,
            batchNo="B2",
            kind="nodes",
            status="failed",
            count=0,
            manifestHash="a" * 64,
        ),
        MirrorJobBatchState(
            releaseId=7,
            batchNo="B5:HAS_PROPERTY",
            kind="edges",
            status="succeeded",
            count=1,
            manifestHash="a" * 64,
        ),
    ]
    result = _run(ports, resume=True)
    statuses = {batch.batchNo: batch.status for batch in result.batches}
    assert statuses["B1"] == "skipped"
    assert statuses["B2"] == "succeeded"
    assert statuses["B5:HAS_PROPERTY"] == "succeeded", (
        "节点批次重跑后，边批次不许跳过（端点变了，边必须跟着重写）"
    )


def test_裁剪批次在计划之后执行且需要确认() -> None:
    ports = _ports()
    result = _run(ports, prune=True, pruneConfirm=True, expectDelete=0)
    assert ports.prunes, "显式开启裁剪就必须真的裁剪"
    assert set(ports.prunes[0]) == {"obj:CLASS:1", "obj:CLASS:2", "obj:PROPERTY:10"}
    kinds = [batch.kind for batch in result.batches]
    assert kinds[-1] == "prune"


# =============================================================================
# 收尾核对
# =============================================================================


def test_写完计数与计划不一致时判失败() -> None:
    ports = _ports()
    ports.nodeCounts = {"class": 99, "property": 1, "metric": 0, "kpi": 0}
    with pytest.raises(MirrorJobError, match="不一致"):
        _run(ports)
    assert ports.states[-1].status == "failed"


def test_成功时状态标记为_materialized_并带计数() -> None:
    ports = _ports()
    _run(ports)
    finalState = ports.states[-1]
    assert finalState.status == "materialized"
    assert finalState.nodeCount == 3
    assert finalState.edgeCount == 2  # HAS_PROPERTY 1 + JOIN 1
    assert finalState.finishedAt


# =============================================================================
# 裁剪的双保险：数量对账
# =============================================================================


def test_裁剪必须给_expect_delete_对账() -> None:
    """不知道会删多少 ⇒ 不许裁剪（Neo4j 无备份，删了不可逆）。"""
    ports = _ports()
    with pytest.raises(MirrorJobBlocked, match="expect-delete"):
        _run(ports, prune=True, pruneConfirm=True)
    assert ports.prunes == []


def test_裁剪数量对不上时拒绝执行() -> None:
    ports = _ports()
    ports.staleCount = 5
    with pytest.raises(MirrorJobBlocked, match="不一致"):
        _run(ports, prune=True, pruneConfirm=True, expectDelete=3)
    assert ports.prunes == []


def test_裁剪数量对得上才执行() -> None:
    ports = _ports()
    ports.staleCount = 3
    _run(ports, prune=True, pruneConfirm=True, expectDelete=3)
    assert len(ports.prunes) == 1
