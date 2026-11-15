"""图谱镜像作业台账的**行 ↔ 值对象**转换单测（P3b D23）。

台账行来自 Neo4j 节点属性（字段可能缺、可能老）⇒ 转换必须：
- **缺字段不读崩**（走默认值）；
- **往返无损**（值对象 → 行 → 值对象 == 原值对象）。
"""

from __future__ import annotations

from app.services.semantic_graph_mirror_job import (
    MirrorJobState,
)

# =============================================================================
# 台账行 ↔ 值对象 的往返转换（脚本用它接 Neo4j 台账）
# =============================================================================


def test_状态标记与台账行可往返转换() -> None:
    from app.services.semantic_graph_mirror_ledger import stateFromRow, stateToRow

    state = MirrorJobState(
        releaseId=7,
        releaseVersion="2026.11.09-1",
        status="materialized",
        manifestHash="a" * 64,
        nodeCount=3,
        edgeCount=2,
        unresolvedCount=1,
        startedAt="t0",
        updatedAt="t1",
        finishedAt="t2",
        error="",
    )
    row = stateToRow(state)
    assert row["release_id"] == 7
    assert row["manifest_hash"] == "a" * 64
    assert row["node_count"] == 3
    assert stateFromRow(row) == state


def test_台账行缺字段时用默认值() -> None:
    """老台账行（字段少）不能读崩 ⇒ 缺字段走默认值。"""
    from app.services.semantic_graph_mirror_ledger import batchFromRow, stateFromRow

    state = stateFromRow({"release_id": 7, "status": "materializing"})
    assert state.status == "materializing"
    assert state.nodeCount == 0
    assert state.manifestHash == ""
    batch = batchFromRow({"release_id": 7, "batch_no": "B1"})
    assert batch.batchNo == "B1"
    assert batch.status == ""
