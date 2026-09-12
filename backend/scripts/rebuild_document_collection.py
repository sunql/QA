"""重建 Milvus document_embeddings 集合。

为什么需要它：``_ensureCollection`` 见到集合已存在就早返回，因此**改字段后
不重建不生效**；且 CollectionSchema 未开 enable_dynamic_field，无法绕过。

安全闸：集合非空时拒绝执行，除非显式设 ``ALLOW_NONEMPTY_REBUILD=1``。

用法（**在宿主机跑，不要用 docker exec**）：
    cd backend && .venv/bin/python scripts/rebuild_document_collection.py

必须用本仓库的新代码执行：容器里的 ``app`` 包在部署前仍是旧版，用
``docker exec qa-backend ...`` 会拿到旧的 ``_documentFields()``，**按旧字段
重建集合**——而且脚本会照常打印「已重建集合」并返回 0，静默产出错 schema。
"""

from __future__ import annotations

import os
import sys

from app.infrastructure.milvus_client import (
    _DOCUMENT_COLLECTION_NAME,
    _connAlias,
    _connect,
    _documentFields,
    _ensureEmbeddingIndex,
)
from pymilvus import Collection, CollectionSchema, utility


def main() -> int:
    _connect()
    alias = _connAlias()
    name = _DOCUMENT_COLLECTION_NAME

    if utility.has_collection(name, using=alias):
        existing = Collection(name, using=alias)
        rowCount = existing.num_entities
        if rowCount > 0 and os.environ.get("ALLOW_NONEMPTY_REBUILD") != "1":
            print(
                f"拒绝执行：集合 {name} 有 {rowCount} 行数据。"
                f"确认要丢弃请设 ALLOW_NONEMPTY_REBUILD=1。",
                file=sys.stderr,
            )
            return 1
        existing.release()
        utility.drop_collection(name, using=alias)
        print(f"已删除旧集合 {name}（{rowCount} 行）")

    schema = CollectionSchema(fields=_documentFields(), description=f"{name} for semantic search")
    collection = Collection(name=name, schema=schema, using=alias)
    _ensureEmbeddingIndex(collection)
    collection.load()
    print(f"已重建集合 {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
