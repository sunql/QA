"""图谱镜像物化作业的**台账行 ↔ 值对象**转换（P3b D23）。

为什么单独一个文件：作业编排（`semantic_graph_mirror_job.py`）关心「怎么跑」，
台账序列化关心「Neo4j 节点长什么样」⇒ 两件事、两个文件；基础设施层只认 dict、
不认 dataclass ⇒ 转换必须有一处 SSOT。

键名与台账节点属性**逐字一致**（`MirrorJobState` / `MirrorJobBatch` 的 CQL SET 列表）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.semantic_graph_mirror_job import (
    MirrorJobBatchState,
    MirrorJobState,
)

# =============================================================================
# 台账行 ↔ 值对象（脚本用它接 Neo4j 台账；基础设施层只认 dict，不认 dataclass）
# =============================================================================


def stateToRow(state: MirrorJobState) -> dict[str, Any]:
    """作业状态标记 → 台账行（键名与台账节点属性一致）。"""
    return {
        "release_id": state.releaseId,
        "release_version": state.releaseVersion,
        "status": state.status,
        "manifest_hash": state.manifestHash,
        "node_count": state.nodeCount,
        "edge_count": state.edgeCount,
        "unresolved_count": state.unresolvedCount,
        "started_at": state.startedAt,
        "updated_at": state.updatedAt,
        "finished_at": state.finishedAt,
        "error": state.error,
    }


def stateFromRow(row: Mapping[str, Any]) -> MirrorJobState:
    """台账行 → 作业状态标记（缺字段走默认值 ⇒ 老台账行不会读崩）。"""
    return MirrorJobState(
        releaseId=int(row.get("release_id") or 0),
        releaseVersion=str(row.get("release_version") or ""),
        status=str(row.get("status") or ""),
        manifestHash=str(row.get("manifest_hash") or ""),
        nodeCount=int(row.get("node_count") or 0),
        edgeCount=int(row.get("edge_count") or 0),
        unresolvedCount=int(row.get("unresolved_count") or 0),
        startedAt=str(row.get("started_at") or ""),
        updatedAt=str(row.get("updated_at") or ""),
        finishedAt=str(row.get("finished_at") or ""),
        error=str(row.get("error") or ""),
    )


def batchToRow(batch: MirrorJobBatchState) -> dict[str, Any]:
    """批次台账 → 台账行。"""
    return {
        "release_id": batch.releaseId,
        "batch_no": batch.batchNo,
        "kind": batch.kind,
        "status": batch.status,
        "count": batch.count,
        "elapsed_ms": batch.elapsedMs,
        "error": batch.error,
        "manifest_hash": batch.manifestHash,
        "updated_at": batch.updatedAt,
    }


def batchFromRow(row: Mapping[str, Any]) -> MirrorJobBatchState:
    """台账行 → 批次台账（缺字段走默认值）。"""
    return MirrorJobBatchState(
        releaseId=int(row.get("release_id") or 0),
        batchNo=str(row.get("batch_no") or ""),
        kind=str(row.get("kind") or ""),
        status=str(row.get("status") or ""),
        count=int(row.get("count") or 0),
        elapsedMs=int(row.get("elapsed_ms") or 0),
        error=str(row.get("error") or ""),
        manifestHash=str(row.get("manifest_hash") or ""),
        updatedAt=str(row.get("updated_at") or ""),
    )
