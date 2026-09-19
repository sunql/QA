"""feat-business-data-reset — Milvus 业务 collection drop。

清理范围（2026-09-19 实测）：
- ontology_embeddings（7315 entries，本体向量）
- wiki_page_embeddings（778 entries，知识库向量）
- document_embeddings（37 entries，文档向量）

保留：
- query_embeddings（409 entries，历史查询向量缓存，删了影响功能）
- entity_mapping_embeddings 不存在（entity_mapping 是 PG 关系表）

只清 3 套业务向量。脚本先 print 列表确认。
"""

from __future__ import annotations

import logging

from pymilvus import MilvusClient

logger = logging.getLogger(__name__)


TARGETS = (
    "ontology_embeddings",
    "wiki_page_embeddings",
    "document_embeddings",
)


def main() -> None:
    c = MilvusClient(uri="http://milvus-standalone:19530")
    before = sorted(c.list_collections())
    logger.info("all collections before: %s", before)

    for name in TARGETS:
        if name not in before:
            logger.warning("skip missing: %s", name)
            continue
        try:
            stats = c.get_collection_stats(name)
            row_count = stats.get("row_count", "?")
        except Exception as e:
            row_count = f"<err: {e}>"
        c.drop_collection(name)
        logger.info("dropped %s (had %s rows)", name, row_count)

    after = sorted(c.list_collections())
    logger.info("remaining collections: %s", after)
    removed = set(before) - set(after)
    logger.info("removed: %s", sorted(removed))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()