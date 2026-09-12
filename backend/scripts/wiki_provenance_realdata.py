"""P0 溯源地基真实数据验证（Harness 开发门禁）。

走**真实摄入链路**：真实 PDF → parse → chunk → embed → Milvus →
document_catalog，然后在链路末端逐项断言溯源信息真的留下来了：

  1. Milvus 里该文档的 chunk 带**正确**的 page_number / paragraph_no —— 页集合必须
     恰好等于样本的 {1, 2}，而不是仅仅「不是哨兵」；并钉死 PDF 的 section_name
     为空串（哨兵）这一契约。只查哨兵会让「页码硬编码为 1」「页映射整体偏移」
     「第 2 页被静默丢弃」三类回归全部绿着通过 —— 本门禁初版就是这么漏的。
  2. document_catalog 有真实 storage_url + 64 位 content_hash（不是 milvus://N_chunks）
  3. MinIO 桶内可读回源文件，且字节与上传完全一致

为什么走真链路而不是只调 parse_document：只验解析器的话，rag_service 把定位
符丢了、把假 URL 写进 catalog，脚本照样全绿 —— 而那两个恰恰是 P0 要修的东西。
门禁必须站在被修的东西的**下游**。

自清：固定 document_id，跑完删掉本脚本写入的 Milvus chunk 与 catalog 行，
反复执行不会给共享集合和正式库留垃圾。

用法：
    docker exec qa-backend python scripts/wiki_provenance_realdata.py
"""

from __future__ import annotations

import asyncio
import io
import re
import sys

from sqlalchemy import text

from app.dependencies import CurrentUser
from app.infrastructure.database import getSessionFactory
from app.infrastructure.milvus_client import deleteDocumentChunks, queryDocumentChunks
from app.infrastructure.object_storage import DEFAULT_BUCKET, getSourceObject
from app.services.rag_service import RagService

GATE_DOC_ID = "DOC-P0-PROVENANCE-GATE"
GATE_FILENAME = "provenance-sample.pdf"


async def main() -> int:
    failures: list[str] = []
    content = _samplePdfBytes()
    factory = getSessionFactory()

    try:
        # --- 1. 走真实摄入链路 ---
        async with factory() as session:
            await _purgeGateCatalogRow(session)
            result = await RagService().ingestDocument(
                session,
                content=content,
                filename=GATE_FILENAME,
                mime_type="application/pdf",
                document_id=GATE_DOC_ID,
                document_name=GATE_FILENAME,
                document_type="OTHER",
                actor=CurrentUser(userId="provenance-gate"),
            )

        print(f"ingest.chunks       = {result['chunks']}")
        print(f"ingest.storage_url  = {result['storage_url']}")
        print(f"ingest.content_hash = {result['content_hash']}")

        # --- 2. Milvus：定位符必须真的落库 ---
        chunks = queryDocumentChunks(GATE_DOC_ID)
        print(f"milvus.chunk_count  = {len(chunks)}")
        if not chunks:
            failures.append("Milvus 里查不到该文档的 chunk")
        else:
            pages = sorted({c["page_number"] for c in chunks})
            print(f"milvus.page_numbers = {pages}")
            if any(c["page_number"] < 1 for c in chunks):
                failures.append("存在没有页码（哨兵 -1）的 chunk")
            if any(c["paragraph_no"] < 1 for c in chunks):
                failures.append("存在没有段号（哨兵 -1）的 chunk")
            # 只查哨兵挡不住三类回归：页码硬编码为 1、页映射整体偏移、第 2 页被静默丢弃。
            # 样本 PDF 两页都有正文，故页集合必须**恰好**是 {1, 2}。
            if pages != [1, 2]:
                failures.append(f"页归属错误：期望 [1, 2]，实际 {pages}")
            # PDF 按设计没有 section_name（那是 DOCX/MD 的定位符），钉死该契约。
            if any(c["section_name"] != "" for c in chunks):
                failures.append("PDF 样本的 section_name 应为空串（哨兵）")

        # --- 3. document_catalog：真实 url + 64 位摘要 ---
        async with factory() as session:
            rows = await session.execute(
                text(
                    "SELECT storage_url, content_hash FROM document_catalog "
                    "WHERE document_id = :d"
                ),
                {"d": GATE_DOC_ID},
            )
            catalog = rows.first()

        if catalog is None:
            failures.append("document_catalog 里没有该文档的行")
        else:
            print(f"catalog.storage_url = {catalog.storage_url}")
            print(f"catalog.content_hash= {catalog.content_hash}")
            if not catalog.storage_url or catalog.storage_url.startswith("milvus://"):
                failures.append(f"storage_url 不是真实对象存储地址：{catalog.storage_url!r}")
            elif not catalog.storage_url.startswith("s3://"):
                failures.append(f"storage_url 形状不对：{catalog.storage_url!r}")
            if not catalog.content_hash or not re.fullmatch(
                r"[0-9a-f]{64}", catalog.content_hash
            ):
                failures.append(f"content_hash 不是 64 位摘要：{catalog.content_hash!r}")
            else:
                # 摘要要与 ingest 返回一致，且对象名的 <hash> 段就是它（内容寻址不脱节）
                if catalog.content_hash != result["content_hash"]:
                    failures.append(
                        "catalog.content_hash 与 ingest 返回不一致："
                        f"{catalog.content_hash!r} != {result['content_hash']!r}"
                    )
                if result["content_hash"] not in (catalog.storage_url or ""):
                    failures.append("storage_url 的对象名不含 content_hash（内容寻址脱节）")

        # --- 4. MinIO：读回的字节必须与上传一致 ---
        s3Prefix = f"s3://{DEFAULT_BUCKET}/"
        if catalog is not None and (catalog.storage_url or "").startswith(s3Prefix):
            objectName = catalog.storage_url[len(s3Prefix):]
            readBack = getSourceObject(objectName)
            if readBack != content:
                failures.append("MinIO 读回内容与上传字节不一致")
            else:
                print(f"minio.read_back     = {len(readBack)} bytes（与上传一致）")
    finally:
        # 自清：门禁不该给正式库留痕。即使断言失败或中途异常，也必须清掉本次写入，
        # 否则下一次运行会撞上残留（共享集合 + 正式库）。
        deleteDocumentChunks(GATE_DOC_ID)
        async with factory() as session:
            await _purgeGateCatalogRow(session)

    if failures:
        print("\n❌ 失败项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n✅ P0 溯源地基真实数据验证通过（已清理本次写入）")
    return 0


async def _purgeGateCatalogRow(session) -> None:
    """删掉门禁行，让脚本可重复执行。"""
    await session.execute(
        text("DELETE FROM document_catalog WHERE document_id = :d"),
        {"d": GATE_DOC_ID},
    )
    await session.commit()


def _samplePdfBytes() -> bytes:
    """用 reportlab 生成两页 PDF（与 test_document_parser 的 _makePdf 同源）。

    不用手搓 PDF 字节：xref 偏移量极易写错，而且写错之后 pypdf 读到的是
    **0 页**而非报错 —— 脚本会以「空结果」的形式静默通过。
    reportlab 是已声明依赖（``reportlab>=4.2.0``，实机 5.0.0，已验证可导入）。

    ⚠️ CJK 字体必须显式指定：默认 Helvetica 编不了中文，reportlab 会静默
    替换成豆腐块，抽出来是 ``■■■■■``。``STSong-Light`` 是自带的 CID 字体。
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for text in ("第一页：供应商准入需注册资本 >= 1000 万", "第二页：质量协议每年复核一次"):
        # 每页都要重设：showPage() 会重置字体状态。
        c.setFont("STSong-Light", 14)
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
