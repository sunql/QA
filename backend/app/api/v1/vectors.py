"""Milvus 向量库只读查询端点。

GET /api/v1/system/vectors/embeddings — 列出向量条目（不含向量值）
GET /api/v1/system/vectors/stats     — 按 type 分组统计
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.infrastructure import milvus_client

router = APIRouter(tags=["system"])

_VALID_TYPES = ("class", "property", "metric")


def _stats(rows: list[dict]) -> dict[str, int]:
    return {t: sum(1 for r in rows if r.get("type") == t) for t in _VALID_TYPES}


@router.get("/vectors/embeddings")
async def listEmbeddings(
    type: str | None = Query(default=None, description="class | property | metric"),
    search: str = Query(default="", description="按 name/alias 包含过滤（不区分大小写）"),
) -> list[dict]:
    """返回 ontology_embeddings 条目（不含向量值），可按 type 过滤、按 name/alias 搜索。"""
    if type is not None and type not in _VALID_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid type: {type!r}")
    rows = milvus_client.listAllEmbeddings()
    if type:
        rows = [r for r in rows if r.get("type") == type]
    if search:
        s = search.lower()
        rows = [
            r for r in rows
            if s in (r.get("name") or "").lower()
            or s in (r.get("alias") or "").lower()
        ]
    # 剔除 embedding 字段，只返回元数据 + dim
    return [
        {
            "ontology_id": r["ontology_id"],
            "type": r["type"],
            "name": r["name"],
            "alias": r.get("alias"),
            "description": r.get("description"),
            "dim": len(r["embedding"]),
        }
        for r in rows
    ]


@router.get("/vectors/stats")
async def getStats() -> dict[str, int]:
    """返回按 type 分组的向量数量统计。"""
    try:
        rows = milvus_client.listAllEmbeddings()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _stats(rows)
