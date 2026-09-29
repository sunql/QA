"""加载 wiki 页面/段落文本：chunk 级走 Milvus；页面级走 PG wiki_page.content。

失败 / 缺失静默忽略，调用方得到 partial 字典而非抛异常。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import WikiPage
from app.infrastructure.milvus_client import (
    _WIKI_PAGE_COLLECTION_NAME, ensureWikiPageCollection,
)

logger = logging.getLogger(__name__)
_PAGE_CONTENT_MAX_CHARS = 4000


def _escape_milvus_value(value: str) -> str:
    """Escape double-quote in Milvus filter expression value."""
    return value.replace('"', '\\"')


async def _searchWikiChunks(page_id: str, chunk_id: str | None) -> list[tuple[str, str, str]]:
    """从 Milvus 按 (page_id, chunk_id?) 取 chunk_text。

    返回 [(page_id, chunk_id, text), ...]；失败返 []。
    """
    try:
        coll = ensureWikiPageCollection()
        page_id_escaped = _escape_milvus_value(page_id)
        if chunk_id is not None:
            chunk_id_escaped = _escape_milvus_value(chunk_id)
            expr = f'page_id == "{page_id_escaped}" && chunk_id == "{chunk_id_escaped}"'
        else:
            expr = f'page_id == "{page_id_escaped}"'
        rows = coll.query(
            expr=expr,
            output_fields=["page_id", "chunk_id", "chunk_text"],
            limit=16,
        )
        return [(r["page_id"], r["chunk_id"], r["chunk_text"]) for r in rows]
    except Exception as e:
        logger.warning("Milvus wiki chunk 查询失败: %s", e)
        return []


class WikiChunkLoader:
    """按 (page_id, chunk_id|None) 列表加载 wiki 文本。"""

    async def loadChunks(
        self,
        session: AsyncSession,
        page_ids: list[str],
        chunk_ids: list[str | None],
    ) -> dict[tuple[str, str], str]:
        """返回 {(page_id, chunk_id|""): text}。"""
        if not page_ids:
            return {}
        result: dict[tuple[str, str], str] = {}
        # 分离 chunk 级 vs 页面级
        chunk_pairs = [
            (p, c) for p, c in zip(page_ids, chunk_ids, strict=True)
            if c is not None
        ]
        page_only = [
            (p, c) for p, c in zip(page_ids, chunk_ids, strict=True)
            if c is None
        ]
        # chunk 级 → Milvus
        for page_id, chunk_id in chunk_pairs:
            rows = await _searchWikiChunks(page_id, chunk_id)
            for p_id, c_id, text in rows:
                if c_id != chunk_id:
                    continue
                result[(p_id, c_id)] = text
        # 页面级 → PG wiki_page.content（截断）
        if page_only:
            unique_pages = {p for p, _ in page_only}
            stmt = select(WikiPage.page_id, WikiPage.content).where(
                WikiPage.page_id.in_(unique_pages)
            )
            rows = (await session.execute(stmt)).all()
            for page_id, content in rows:
                content = content or ""
                result[(page_id, "")] = content[:_PAGE_CONTENT_MAX_CHARS]
        return result
