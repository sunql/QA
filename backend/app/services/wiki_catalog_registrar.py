"""Wiki 摄入来源在 document_catalog 的登记。

单独成文件而非塞进 wiki_import_service：后者已 520 行，接近 800 行上限；
登记逻辑与导入编排无共同状态，拆开后各自可测。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DocumentType
from app.domain.models import DocumentCatalog

logger = logging.getLogger(__name__)

# document_id 的业务前缀，便于一眼区分 wiki 摄入来源与上传文档。
_WIKI_DOC_PREFIX = "DOC-WIKI-"


class WikiCatalogRegistrar:
    """按 content_hash 幂等登记 document_catalog。"""

    async def upsertByContentHash(
        self,
        session: AsyncSession,
        *,
        document_name: str,
        content_hash: str,
        storage_url: str | None,
        actor: object | None = None,
    ) -> DocumentCatalog:
        """同 hash 已存在则返回既有行，否则新建。

        document_type 固定为 ``DocumentType.OTHER``：wiki 导入的是知识条目，
        不属于 CONTRACT / SOP 等既有文档类别。**不能**透传 source_type
        （那里的取值是 MARKDOWN/PDF 之类的来源格式），它既不是 DocumentType
        的合法值，语义上也完全是另一回事。
        """
        stmt = select(DocumentCatalog).where(DocumentCatalog.content_hash == content_hash)
        existing = (await session.execute(stmt)).scalars().first()
        if existing is not None:
            logger.info("document_catalog 已存在同 hash 行，跳过登记: %s", content_hash[:12])
            return existing

        entity = DocumentCatalog(
            document_id=f"{_WIKI_DOC_PREFIX}{content_hash[:12].upper()}",
            document_name=document_name,
            document_type=DocumentType.OTHER,
            storage_url=storage_url,
            content_hash=content_hash,
        )
        session.add(entity)
        await session.flush()
        return entity
