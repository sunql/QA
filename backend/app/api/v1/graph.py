"""Neo4j 图库只读查询端点。

GET /api/v1/system/graph/nodes              — 列出节点
GET /api/v1/system/graph/nodes/{label}/{id}/relationships — 获取关联
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.infrastructure import neo4j_client

router = APIRouter(tags=["system"])

_VALID_LABELS = ("Class", "Property", "Metric")


def _stripNode(n: dict[str, Any]) -> dict[str, Any]:
    """提取已知字段，丢弃内部 neo4j meta。"""
    known = {
        "id", "name", "alias", "description", "sourceTable",
        "sourceColumn", "dataType", "isPrimaryKey", "isForeignKey",
        "formula", "aggFunction",
    }
    return {k: v for k, v in n.items() if k in known}


@router.get("/graph/nodes")
async def listNodes(
    label: str = Query(description="Node label: Class | Property | Metric"),
    search: str = Query(default="", description="按 name/alias 包含过滤（不区分大小写）"),
) -> list[dict]:
    """返回指定 label 的节点列表，可按 name/alias 过滤。"""
    if label not in _VALID_LABELS:
        raise HTTPException(status_code=422, detail=f"Invalid label: {label!r}")
    all_nodes = neo4j_client.listNodesByLabel(label)
    if search:
        s = search.lower()
        all_nodes = [
            n for n in all_nodes
            if s in (n.get("name") or "").lower()
            or s in (n.get("alias") or "").lower()
        ]
    return [_stripNode(n) for n in all_nodes]


@router.get("/graph/nodes/{label}/{nodeId}/relationships")
async def getRelationships(
    label: str,
    nodeId: int,
) -> list[dict]:
    """返回指定节点的出边，含 relType / targetId / targetName / targetLabel。"""
    if label not in _VALID_LABELS:
        raise HTTPException(status_code=422, detail=f"Invalid label: {label!r}")
    try:
        return neo4j_client.getNodeRelationships(label, nodeId)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
