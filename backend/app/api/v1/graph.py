"""Neo4j 图库只读查询端点。

GET /api/v1/system/graph/nodes              — 列出节点
GET /api/v1/system/graph/nodes/{label}/{unifiedId}/relationships — 获取关联
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import getCurrentUser
from app.infrastructure import neo4j_client

router = APIRouter(tags=["system"], dependencies=[Depends(getCurrentUser)])

_VALID_LABELS = ("Class", "Property", "Metric")


def _stripNode(n: dict[str, Any]) -> dict[str, Any]:
    """提取已知字段，丢弃内部 neo4j meta；`unified_id` 对外改名 `unifiedId`。

    节点主键是 unified_id（M0 起），必须下发 —— 否则前端拿不到节点标识，
    关系查询无从发起。改写为 camelCase 是为了与全站 JSON 契约一致。
    """
    known = (
        "id", "name", "alias", "description", "sourceTable",
        "sourceColumn", "dataType", "isPrimaryKey", "isForeignKey",
        "formula", "aggFunction", "unified_id",
    )
    stripped = {k: v for k, v in n.items() if k in known}
    unifiedId = stripped.pop("unified_id", None)
    if unifiedId is not None:
        stripped["unifiedId"] = unifiedId
    return stripped


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


@router.get("/graph/nodes/{label}/{unifiedId}/relationships")
async def getRelationships(
    label: str,
    unifiedId: str,
) -> list[dict]:
    """返回指定节点的出边，含 relType / targetUid / targetName / targetLabel。

    路径参数是 **unified_id**（形如 `obj:CLASS:9`，含冒号需调用方 encode）。
    契约从 PG 整数 id 改成 unified_id 是刻意的：M0 之后图节点主键就是它，
    继续用 PG id 需要在服务端反查或解析字符串格式，等于把 M0 拆掉的耦合装回去。
    """
    if label not in _VALID_LABELS:
        raise HTTPException(status_code=422, detail=f"Invalid label: {label!r}")
    try:
        return neo4j_client.getNodeRelationships(label, unifiedId)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
