"""回填 document_catalog 的 0163 元数据（mime_type / file_size / preview_kind）。

背景：0163 给目录加了「原文阅览」所需的三列，但**既有行**（0163 之前入库的
文档）这三列是 NULL ⇒ 前端拿不到预览类别、也显示不出体积。

回填口径（**只用真实可得的证据**，不猜）：
- ``preview_kind`` / ``mime_type``：从 ``storage_url`` 里的**对象名扩展名**推断
  （storage_url = ``s3://bucket/sources/xx/<hash>/<原文件名>``）；
- ``file_size``：**实际去对象存储读**对象大小（不是猜）——读不到就留 NULL
  （宁缺勿造；体积只是展示用）。

幂等：只更新三列仍为 NULL 的行（已回填的不动 ⇒ 重复跑零副作用）。

用法：
    cd backend && uv run python -m scripts.backfill_document_preview_metadata
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.domain.models import DocumentCatalog  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.document_preview_kinds import kindFor, mimeFor  # noqa: E402


def objectNameFromStorageUrl(storageUrl: str) -> str | None:
    """``s3://bucket/sources/xx/<hash>/<name>`` → ``sources/xx/<hash>/<name>``。"""
    if not storageUrl.startswith("s3://"):
        return None
    rest = storageUrl[len("s3://"):]
    slash = rest.find("/")
    if slash == -1:
        return None
    return unquote(rest[slash + 1:])


def filenameFromStorageUrl(storageUrl: str) -> str:
    """从 storage_url 取原文件名（最后一段）。"""
    name = objectNameFromStorageUrl(storageUrl)
    if not name:
        return ""
    return name.rsplit("/", 1)[-1]


async def _objectSize(objectName: str) -> int | None:
    """读对象大小；读不到 ⇒ None（不猜）。"""
    from app.infrastructure.object_storage import _getClient, DEFAULT_BUCKET

    try:
        client = _getClient()
        stat = client.stat_object(DEFAULT_BUCKET, objectName)
        return int(stat.size)
    except Exception:  # noqa: BLE001 —— 读不到就留空，不阻塞回填
        return None


async def backfill(session: AsyncSession) -> dict[str, int]:
    """回填三列；返回统计。"""
    # 取「预览类别为空」**或**「体积为空」的行：
    # 体积可能因为第一次跑时 MinIO 不可达而漏掉（预览类别已填 ⇒ 整行被跳过），
    # 补跑要能把体积补上，不能因为 preview_kind 已填就整行跳过。
    rows = (
        await session.execute(
            select(DocumentCatalog).where(
                DocumentCatalog.preview_kind.is_(None)
                | DocumentCatalog.file_size.is_(None)
            )
        )
    ).scalars().all()
    stats = {"scanned": len(rows), "updated": 0, "sizeResolved": 0, "skipped": 0}
    for row in rows:
        if not row.storage_url:
            stats["skipped"] += 1
            continue
        filename = filenameFromStorageUrl(row.storage_url)
        if not filename:
            stats["skipped"] += 1
            continue
        if row.preview_kind is None:
            row.preview_kind = kindFor(None, filename)
            row.mime_type = mimeFor(None, filename)
        objectName = objectNameFromStorageUrl(row.storage_url)
        size = await _objectSize(objectName) if objectName else None
        if size is not None:
            row.file_size = size
            stats["sizeResolved"] += 1
        stats["updated"] += 1
    await session.commit()
    return stats


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        stats = await backfill(session)
    print(
        "[backfill_document_preview_metadata] "
        f"扫描 {stats['scanned']} 行 / 更新 {stats['updated']} 行 / "
        f"取到体积 {stats['sizeResolved']} / 跳过 {stats['skipped']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
