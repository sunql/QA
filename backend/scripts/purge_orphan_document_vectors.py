"""清理**孤儿文档向量**：Milvus 里 document_id 已不在 `document_catalog` 的 chunk。

背景：2026-11-22 之前 `deleteDocument` 只删 DB 行、不删 Milvus ⇒ 历史删除留下的
向量仍可被 RAG 检索到（知识泄漏 + 结果指向不存在的文档）。删除路径已修复；
本脚本用于**清理历史孤儿**。

用法（在生产 backend 容器内执行，Milvus 可达）：

    python -m scripts.purge_orphan_document_vectors            # 只报告（默认）
    python -m scripts.purge_orphan_document_vectors --apply    # 真删孤儿

退出码：0 = 正常；2 = 连接/查询失败。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.domain.models import DocumentCatalog  # noqa: E402
from app.infrastructure.database import ownSession  # noqa: E402
from app.infrastructure.milvus_client import (  # noqa: E402
    deleteDocumentChunks,
    ensureDocumentCollection,
)

_QUERY_LIMIT = 1000


def orphanDocumentIds(
    milvusIds: set[str], catalogIds: set[str]
) -> list[str]:
    """Milvus 有、catalog 没有的 document_id（纯函数，便于单测）。"""
    return sorted(milvusIds - catalogIds)


def _listMilvusDocumentIds() -> set[str]:
    """枚举 document collection 里出现过的 document_id（分页 query）。"""
    collection = ensureDocumentCollection()
    collection.load()
    ids: set[str] = set()
    offset = 0
    while True:
        rows = collection.query(
            expr='document_id != ""',
            output_fields=["document_id"],
            limit=_QUERY_LIMIT,
            offset=offset,
        )
        if not rows:
            break
        for row in rows:
            value = row.get("document_id")
            if isinstance(value, str) and value:
                ids.add(value)
        if len(rows) < _QUERY_LIMIT:
            break
        offset += _QUERY_LIMIT
    return ids


async def _run(apply: bool) -> int:
    try:
        milvusIds = _listMilvusDocumentIds()
    except Exception as exc:  # noqa: BLE001 - 探针：打印原因并退出码 2
        print(f"[purge] 读取 Milvus 失败：{exc}", file=sys.stderr)
        return 2
    async with ownSession() as session:
        catalogIds = set(
            (await session.execute(select(DocumentCatalog.document_id))).scalars().all()
        )
    orphans = orphanDocumentIds(milvusIds, catalogIds)
    print(
        f"[purge] Milvus 文档数={len(milvusIds)}  catalog 文档数={len(catalogIds)}  "
        f"孤儿={len(orphans)}"
    )
    for documentId in orphans[:20]:
        print(f"  - {documentId}")
    if len(orphans) > 20:
        print(f"  … 等 {len(orphans)} 个")
    if not apply:
        print("[purge] 只报告；加 --apply 才会删除")
        return 0
    deleted = 0
    for documentId in orphans:
        try:
            deleteDocumentChunks(documentId)
            deleted += 1
        except Exception as exc:  # noqa: BLE001 - 单个失败不中断，最后汇总
            print(f"[purge] 删除 {documentId} 失败：{exc}", file=sys.stderr)
    print(f"[purge] 已删除孤儿向量：{deleted}/{len(orphans)}")
    return 0 if deleted == len(orphans) else 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真删（默认只报告）")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.apply)))


if __name__ == "__main__":
    main()
