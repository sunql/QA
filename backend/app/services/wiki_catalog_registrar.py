"""Wiki 摄入来源在 document_catalog 的登记。

单独成文件而非塞进 wiki_import_service：后者已 520 行，接近 800 行上限；
登记逻辑与导入编排无共同状态，拆开后各自可测。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pgInsert
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
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
        actor: CurrentUser,
    ) -> DocumentCatalog | None:
        """同 hash 已存在则返回既有行，否则新建。**可能返回 ``None``**（见下）。

        用 ``INSERT ... ON CONFLICT DO NOTHING`` 而非 SELECT-then-INSERT：
        后者是 check-then-act 竞态 —— 两个并发登记同一内容都通过 SELECT，
        败者会在唯一索引 ``uq_document_catalog_content_hash`` 上炸 IntegrityError。
        原子 upsert 后冲突由数据库裁决，胜者插入、败者走冲突分支。

        **冲突分支的回查不保证读得到胜者的行**：默认 READ COMMITTED 下，败者的
        ``SELECT`` 若早于胜者 ``COMMIT``，会查不到任何行而返回 ``None``。
        因此返回类型是 ``DocumentCatalog | None``，调用方**必须容忍 None**
        （今天的唯一调用方 ``wiki_import_service`` 只取 ``storage_url``、完全
        丢弃返回值，故无影响）。要拿到非空结果需靠重试或 ``SELECT ... FOR
        SHARE``，本变更未做。

        document_type 固定为 ``DocumentType.OTHER``：wiki 导入的是知识条目，
        不属于 CONTRACT / SOP 等既有文档类别。**不能**透传 source_type
        （那里的取值是 MARKDOWN/PDF 之类的来源格式），它既不是 DocumentType
        的合法值，语义上也完全是另一回事。

        owner 由 ``actor.departments[0]`` 派生（entity_mapping 同模式），
        不接受 client 声明，防止越权；``departments`` 为空 → ``owner=None``。
        """
        owner = actor.departments[0] if actor.departments else None
        insert_stmt = (
            pgInsert(DocumentCatalog)
            .values(
                document_id=f"{_WIKI_DOC_PREFIX}{content_hash[:12].upper()}",
                document_name=document_name,
                document_type=DocumentType.OTHER,
                owner=owner,
                storage_url=storage_url,
                content_hash=content_hash,
            )
            .on_conflict_do_nothing(index_elements=["content_hash"])
            .returning(DocumentCatalog.id)
        )
        inserted_id = (await session.execute(insert_stmt)).scalar_one_or_none()

        if inserted_id is not None:
            return await session.get(DocumentCatalog, inserted_id)

        # 冲突：同 hash 已存在 → 返回既有行（幂等语义与旧 SELECT 分支一致）。
        logger.info("document_catalog 已存在同 hash 行，跳过登记: %s", content_hash[:12])
        return (
            await session.execute(
                select(DocumentCatalog).where(
                    DocumentCatalog.content_hash == content_hash
                )
            )
        ).scalars().first()
