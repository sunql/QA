"""文档文本质量**体检报告**（只提示，不判罚、不写库）。

设计变更（2026-11-22，用户裁定）：**是否参与 AI 上下文由人工开关决定**
（`document_catalog.ai_context_enabled`），本脚本只是帮运营**找出可疑文档**，
不自动关、不写库。

为什么不自动判：实测自动规则反复误判——中文文档里的编码（`TH-MDM-01`/`SUS304`）
会让"大写词比例"冲到 87%–95%，把正常文档全判成乱码；放宽阈值又漏掉真乱码。
**判定规则不可靠时，把决定权交给运营。**

用法::

    python -m scripts.audit_document_text_quality

输出：每份文档的「源文件文本」「向量库 chunk 文本」两项指标 + 是否可疑；
对可疑文档给出**建议动作**（人工开关的调用示例）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.domain.models import DocumentCatalog  # noqa: E402
from app.services.document_text_quality import (  # noqa: E402
    QUALITY_OK,
    QUALITY_PARSE_FAILED,
    judgeTextQuality,
)


async def _inspect(rows: list[DocumentCatalog]) -> list[dict]:
    """逐文档体检：源文件解析 + 向量库 chunk 文本（**只读**）。"""
    from app.infrastructure.milvus_client import queryDocumentChunks
    from app.services.document_parser import parse_document
    from app.services.document_source_service import readSourceBytes

    results: list[dict] = []
    for row in rows:
        record = {
            "documentId": row.document_id,
            "name": row.document_name,
            "enabled": getattr(row, "ai_context_enabled", True),
            "note": getattr(row, "ai_context_note", None),
            "quality": QUALITY_PARSE_FAILED,
            "reason": "",
            "chunks": 0,
            "chunkQuality": None,
            "chunkReason": "",
        }
        data = await readSourceBytes(row.storage_url)
        if data is None:
            record["reason"] = "无源文件"
        else:
            try:
                blocks = await parse_document(
                    data, row.mime_type or "application/octet-stream", row.document_name
                )
                text = "\n".join(block.text for block in blocks)
                record["quality"], record["reason"] = judgeTextQuality(text)
            except Exception as exc:  # noqa: BLE001 —— 体检要覆盖「解析炸了」
                record["reason"] = f"解析失败：{type(exc).__name__}"

        try:
            chunks = queryDocumentChunks(row.document_id)
        except Exception:  # noqa: BLE001
            chunks = []
        record["chunks"] = len(chunks)
        if chunks:
            joined = " ".join(str(c.get("chunk_text") or "") for c in chunks)
            record["chunkQuality"], record["chunkReason"] = judgeTextQuality(joined)
        results.append(record)
    return results


def _suspicious(record: dict) -> bool:
    return record["quality"] != QUALITY_OK or record["chunkQuality"] not in (None, QUALITY_OK)


def _printReport(results: list[dict]) -> None:
    print(
        f"{'document_id':<26}{'AI':<4}{'源文本':<14}{'chunk':>6}  {'向量':<14}说明"
    )
    print("-" * 118)
    for record in results:
        aiFlag = "开" if record["enabled"] else "关"
        vectorQuality = record["chunkQuality"] or "-"
        note = record["reason"]
        if record["chunkQuality"] not in (None, QUALITY_OK):
            note += f" | 向量：{record['chunkReason']}"
        if not record["enabled"] and record["note"]:
            note += f" | 关闭原因：{record['note']}"
        mark = "⚠️" if _suspicious(record) else "  "
        print(
            f"{mark}{record['documentId']:<24}{aiFlag:<4}{record['quality']:<14}"
            f"{record['chunks']:>6}  {vectorQuality:<14}{note}"
        )

    candidates = [r for r in results if _suspicious(r) and r["enabled"]]
    print(f"\n合计 {len(results)} 份；**可疑且当前仍启用** {len(candidates)} 份")
    if candidates:
        print("\n建议（人工确认后执行，本脚本不会自动改动）：")
        print("  PUT /api/v1/documents/{id}  {\"aiContextEnabled\": false, \"aiContextNote\": \"文本疑似乱码\"}")
        for record in candidates:
            print(f"  - {record['documentId']}（{record['name']}）：{record['reason']}")


async def main() -> None:
    from app.infrastructure.database import getSessionFactory

    factory = getSessionFactory()
    async with factory() as session:
        rows = list((await session.execute(select(DocumentCatalog))).scalars().all())
        results = await _inspect(rows)
    _printReport(results)


if __name__ == "__main__":
    asyncio.run(main())
