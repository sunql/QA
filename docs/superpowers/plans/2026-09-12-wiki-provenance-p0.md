# P0 溯源地基 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每段入库文本携带页码/章节/段号定位符，并把源文件真实留存到对象存储，使后续证据链有出处可填。

**Architecture:** 在摄入链路上补三处丢失点：(1) `parse_document` 从返回扁平 `str` 改为返回带定位的 `list[TextBlock]`；(2) `Chunk` 用既有的未使用 `metadata` 字段承载定位符，落进 Milvus 新字段；(3) 新增 MinIO 客户端把源文件存成内容寻址对象，`document_catalog` 写真实 `storage_url` + `content_hash`。全程无 PG 迁移。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 async / pypdf / python-docx / pymilvus 2.4.6 / MinIO (S3 兼容) / pytest

## Global Constraints

- **测试必须用真实 PostgreSQL + 完整 API 链路，禁止 sqlite**（`Harness/rules/测试规范.md`）。
- 集成测试需要 `TEST_DATABASE_URL`，缺失时应 fail-fast。
- 集成测试运行命令：
  `cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest <path> -q`
- **Alembic 默认打 prod**（`alembic/env.py` 只认 `DATABASE_URL`，默认 5432/prod）；本计划**不产生任何 PG 迁移**。
- 本计划**包含一次 Milvus 集合重建**。执行前必须复查 `document_embeddings` 仍为空。
- 文件 200-400 行宜，上限 800；函数 < 50 行；嵌套 ≤ 4 层。
- 不可变数据：返回新对象，禁止原地修改。
- **错误不得静默吞掉**：MinIO 不可用必须显式失败（本计划修掉一处既有 `except: pass`）。
- 注释与 docstring 用中文（与代码库一致）。
- Conventional Commits。
- **⚠️ 部署方式变更**：本计划新增 Python 依赖 `minio`。`scripts/deploy_backend.sh` 只 `docker cp` `app/`+`scripts/`+`alembic/`，**不安装依赖** —— 它无法部署本计划。必须走完整镜像重建（见 Task 7）。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/app/services/document_parser.py` | 改 | 新增 `TextBlock`；`parse_document` 返回 `list[TextBlock]` |
| `backend/app/services/chunk_splitter.py` | 改 | `split_by_paragraphs` 吃 `list[TextBlock]`；定位符写进 `Chunk.metadata` |
| `backend/app/infrastructure/milvus_client.py` | 改 | `_documentFields()` 加 3 个定位符字段；`insertDocumentChunks` 对应加列 |
| `backend/scripts/rebuild_document_collection.py` | 建 | 集合重建（`_ensureCollection` 已存在即早返回，改字段不重建不生效） |
| `backend/app/infrastructure/object_storage.py` | 建 | MinIO 客户端：`ensureBucket` / `putSourceObject` / `getSourceObject` / `hashContent` / `buildSourceObjectName` |
| `backend/app/services/rag_service.py` | 改 | 存源文件；写真实 `storage_url` + `content_hash`；消除静默失败 |
| `backend/app/api/v1/wiki_import.py` | 改 | `preview-file` 留存源文件（解析成功后）+ 契约 docstring 改写 |
| `backend/app/services/wiki_import_service.py` | 改 | `parseFile` 适配新返回类型；新增 `persistSourceFile`（留存 + 登记） |
| `backend/app/services/wiki_catalog_registrar.py` | 建 | 按 `content_hash` 幂等登记 `document_catalog` |
| `backend/app/services/messages_zh.py` | 改 | 新增 `MSG_WIKI_IMPORT_SOURCE_STORE_FAILED` |
| `backend/app/tests/integration/test_wiki_import_catalog.py` | 建 | 假 MinIO + 真实 PG：wiki 上传留存与幂等 |
| `backend/app/tests/unit/test_document_parser.py` | 改 | 断言定位符 |
| `backend/app/tests/unit/test_chunk_splitter.py` | 改 | 签名变更 + 定位符断言 |
| `backend/app/tests/unit/test_rag_service.py` | 改 | mock 适配新返回类型 |
| `backend/app/tests/unit/test_object_storage.py` | 建 | 对象存储单测 |
| `backend/app/tests/integration/test_rag_ingest_provenance.py` | 建 | 端到端：上传 → Milvus 带定位符 → catalog 有真实 url/hash |
| `backend/pyproject.toml` + `backend/uv.lock` | 改 | 加 `minio` 依赖 |
| `docker/docker-compose.yml` | 改 | 加 `qa-objects` 服务 + `objects_data` 卷 |
| `scripts/backup_objects.sh` | 建 | MinIO 数据备份（手动） |
| `backend/scripts/wiki_provenance_realdata.py` | 建 | 真实数据验证（Harness 开发门禁） |
| `Harness/changes/feat-wiki-provenance/summary.md` | 建 | 九段变更记录（SSOT） |
| `Harness/rules/数据存储防护.md` | 改 | 卷清单加 `objects_data` |

---

## Task 1: `TextBlock` 与 `parse_document` 返回类型变更

**Files:**
- Modify: `backend/app/services/document_parser.py`（全文重写，82 行 → 约 120 行）
- Test: `backend/app/tests/unit/test_document_parser.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `TextBlock` frozen dataclass，字段 `text: str`、`page_number: int | None`、`section_name: str | None`、`paragraph_no: int | None`
  - `async def parse_document(content: bytes, mime_type: str, filename: str) -> list[TextBlock]`
  - `_splitParagraphs(text: str) -> list[str]`（模块私有）
  - 异常 `DocumentParserError` / `UnsupportedFileTypeError` **签名不变**

**定位符语义**（本任务定义，后续任务依赖）：

| 字段 | PDF | DOCX | MD/TXT |
|---|---|---|---|
| `page_number` | 1-based 页码 | `None` | `None` |
| `section_name` | `None` | Heading 样式文本 | `#` 标题文本 |
| `paragraph_no` | 页内 1-based 段号 | 文档内 1-based 段号 | 文档内 1-based 段号 |

> **保留既有设计**：`UnsupportedFileTypeError` 继承 `DocumentParserError` 但**不得合并**——原注释已写明理由（"换格式即可" vs "换文件"，合并会让用户拿着损坏的 PDF 反复换扩展名）。此约束在重写中必须保留。

- [ ] **Step 1: 写失败测试**

替换 `backend/app/tests/unit/test_document_parser.py` 全文：

```python
"""document_parser 定位符单测（P0 溯源地基）。"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.services.document_parser import (
    DocumentParserError,
    TextBlock,
    UnsupportedFileTypeError,
    parse_document,
)


class TestPlainText:
    """MD / TXT：段落 + 章节识别。"""

    @pytest.mark.asyncio
    async def test_plain_text_utf8(self) -> None:
        blocks = await parse_document(b"hello world", "text/plain", "a.txt")
        assert len(blocks) == 1
        assert blocks[0].text == "hello world"
        assert blocks[0].page_number is None
        assert blocks[0].paragraph_no == 1
        assert blocks[0].section_name is None

    @pytest.mark.asyncio
    async def test_markdown_sections(self) -> None:
        md = "# 第一章\n\n第一段内容。\n\n## 1.1 小节\n\n第二段内容。"
        blocks = await parse_document(md.encode("utf-8"), "text/markdown", "a.md")
        # 标题本身也保留为块（不丢文本），且携带它开启的 section
        assert [b.text for b in blocks] == ["# 第一章", "第一段内容。", "## 1.1 小节", "第二段内容。"]
        assert blocks[0].section_name == "第一章"
        assert blocks[1].section_name == "第一章"
        assert blocks[2].section_name == "1.1 小节"
        assert blocks[3].section_name == "1.1 小节"
        assert [b.paragraph_no for b in blocks] == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_blank_lines_separate_paragraphs(self) -> None:
        blocks = await parse_document(b"a\n\n\nb", "text/plain", "a.txt")
        assert [b.text for b in blocks] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_list(self) -> None:
        blocks = await parse_document(b"   \n\n  ", "text/plain", "a.txt")
        assert blocks == []


class TestUnsupported:
    """不支持的类型与损坏文件必须可区分。"""

    @pytest.mark.asyncio
    async def test_unsupported_type_raises(self) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            await parse_document(b"x", "application/x-msdownload", "a.exe")

    @pytest.mark.asyncio
    async def test_corrupt_pdf_raises_document_parser_error(self) -> None:
        with pytest.raises(DocumentParserError) as excinfo:
            await parse_document(b"not a pdf", "application/pdf", "a.pdf")
        assert not isinstance(excinfo.value, UnsupportedFileTypeError)


class TestPdf:
    """PDF：每页一段块，段号在页内重新计数。"""

    @pytest.mark.asyncio
    async def test_pdf_page_numbers_are_1_based_and_continuous(self) -> None:
        pdf_bytes = _makePdf(["第一页内容", "第二页内容"])
        blocks = await parse_document(pdf_bytes, "application/pdf", "a.pdf")
        assert [b.page_number for b in blocks] == [1, 2]
        assert [b.paragraph_no for b in blocks] == [1, 1]
        assert [b.text for b in blocks] == ["第一页内容", "第二页内容"]

    @pytest.mark.asyncio
    async def test_pdf_fallback_by_extension(self) -> None:
        pdf_bytes = _makePdf(["内容"])
        blocks = await parse_document(pdf_bytes, "application/octet-stream", "a.pdf")
        assert len(blocks) == 1
        assert blocks[0].page_number == 1


class TestDocx:
    """DOCX：段号在文档内递增，Heading 样式开章节。"""

    @pytest.mark.asyncio
    async def test_docx_paragraph_numbers(self) -> None:
        docx_bytes = _makeDocx(["第一章", "正文一", "正文二"])
        blocks = await parse_document(
            docx_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "a.docx",
        )
        assert [b.text for b in blocks] == ["第一章", "正文一", "正文二"]
        assert [b.paragraph_no for b in blocks] == [1, 2, 3]
        assert all(b.page_number is None for b in blocks)

    @pytest.mark.asyncio
    async def test_docx_heading_sets_section(self) -> None:
        docx_bytes = _makeDocx(["概述", "正文内容"], headingIndexes={0})
        blocks = await parse_document(docx_bytes, "application/octet-stream", "a.docx")
        assert blocks[0].section_name == "概述"
        assert blocks[1].section_name == "概述"

    @pytest.mark.asyncio
    async def test_docx_style_id_heading_without_styles_part(self) -> None:
        """真实 Word 文件的样式解析依赖 styles.xml；本夹具不含该部件，
        python-docx 会把所有段落都报成 Normal。因此 heading 判定不能只看
        ``para.style.name``，必须直读 ``w:pStyle``（见 _parseDocx）。"""
        docx_bytes = _makeDocx(["第一章", "正文"], headingIndexes={0})
        blocks = await parse_document(docx_bytes, "application/octet-stream", "a.docx")
        assert blocks[0].section_name == "第一章"


class TestTextBlockImmutability:
    """TextBlock 必须是 frozen（项目不可变数据约束）。"""

    def test_textblock_is_frozen(self) -> None:
        block = TextBlock(text="x", page_number=None, section_name=None, paragraph_no=1)
        with pytest.raises(Exception):
            block.text = "y"  # type: ignore[misc]


# ---------- 测试夹具：构最小 PDF / DOCX ----------


def _makePdf(pageTexts: list[str]) -> bytes:
    """用 reportlab 生成多页 PDF（每页一行文字）。

    不用手搓 PDF 字节：xref 偏移量极易写错，而且写错之后 pypdf 读到的是
    **0 页**而非报错 —— 测试会以「空结果」的形式静默通过。reportlab 是
    pyproject.toml 里已声明的正式依赖。

    ⚠️ **必须显式指定 CJK 字体。** reportlab 默认的 Helvetica 编不了中文，
    它不报错，而是把每个汉字替换成一个豆腐块 —— pypdf 抽出来是 `■■■■■`。
    于是「夹具画中文、断言比中文」两个动作互相矛盾，任何 `_parsePdf` 实现
    都过不了这条用例，而失败信息还完全指向不到字体上。
    `STSong-Light` 是 reportlab 自带的 CID 字体，不需要额外依赖或字体文件。
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for text in pageTexts:
        # 每页都要重设：showPage() 会重置字体状态，只在循环外设一次不够。
        c.setFont("STSong-Light", 14)
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def _makeDocx(paragraphs: list[str], headingIndexes: set[int] | None = None) -> bytes:
    """手工构造最小 DOCX（OOXML 包）。

    **不含 word/styles.xml** —— 这是有意的：真实 Word 文件一定带该部件，
    但若 _parseDocx 依赖 ``para.style.name`` 判标题，本夹具会因样式解析失
    败把每段都报成 Normal，从而把这个错误依赖暴露出来。直读 ``w:pStyle``
    则在有/无 styles.xml 两种情况下都成立。
    """
    headingIndexes = headingIndexes or set()
    bodyParts: list[str] = []
    for i, text in enumerate(paragraphs):
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        style = (
            '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            if i in headingIndexes
            else ""
        )
        bodyParts.append(
            f'<w:p>{style}<w:r><w:t xml:space="preserve">{escaped}</w:t></w:r></w:p>'
        )
    documentXml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(bodyParts)}</w:body></w:document>"
    )
    contentTypes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", contentTypes)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", documentXml)
    return buf.getvalue()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_document_parser.py -q
```

Expected: FAIL — `ImportError: cannot import name 'TextBlock' from 'app.services.document_parser'`

- [ ] **Step 3: 实现**

替换 `backend/app/services/document_parser.py` 全文：

```python
"""文档解析器：把 PDF/DOCX/MD 解析成带定位信息的文本块。

P0 溯源地基：原实现返回扁平字符串，PDF 的页码与 DOCX 的段落结构在
``"\\n".join(...)`` 处被丢弃，导致下游证据链无出处可填。本实现改为返回
``list[TextBlock]``，把定位符一路带到 Chunk 与 Milvus。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class DocumentParserError(Exception):
    """解析失败。"""
    pass


class UnsupportedFileTypeError(DocumentParserError):
    """文件类型不在支持范围内。

    与「文件损坏」分开：前者换格式即可，后者要换文件。调用方要据此给出
    **不同**的提示，合并成一句会让用户拿着一个损坏的 PDF 反复换扩展名。
    """
    pass


@dataclass(frozen=True)
class TextBlock:
    """带定位信息的最小文本单元。

    定位符语义（各格式不同，下游按此解释）：

    - PDF：``page_number`` 为 1-based 页码，``paragraph_no`` 为**页内**段号
    - DOCX：``paragraph_no`` 为文档内段号，``page_number`` 为 None
    - MD/TXT：``section_name`` 由 ``#`` 标题识别，``paragraph_no`` 为文档内段号
    """

    text: str
    page_number: int | None
    section_name: str | None
    paragraph_no: int | None


# 空行（含仅空白行）分段。PDF 的 extract_text 常在同一段内插换行，
# 按单个 \n 切会把一段切成多块，故只认空行。
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


async def parse_document(content: bytes, mime_type: str, filename: str) -> list[TextBlock]:
    """解析文档内容为带定位信息的文本块列表。

    Args:
        content: 文件字节内容
        mime_type: MIME 类型（如 application/pdf）
        filename: 原始文件名（用于扩展名推断）

    Returns:
        文本块列表；空文档返回空列表

    Raises:
        UnsupportedFileTypeError: 格式不支持
        DocumentParserError: 文件损坏或解析失败
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if mime_type == "text/plain" or ext in ("txt", "md", "markdown"):
        return _parsePlainText(content.decode("utf-8", errors="replace"))

    if mime_type == "application/pdf" or ext == "pdf":
        return _parsePdf(content)

    # 只认 docx（OOXML 包），**不含**老式二进制 ``.doc``：python-docx 读不了
    # 后者，会抛 ``Package not found`` —— 那是个误导性的「解析失败」，真相是
    # 「这种格式根本不支持」。
    if mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ) or ext == "docx":
        return _parseDocx(content)

    raise UnsupportedFileTypeError(f"Unsupported file type: {mime_type} ({filename})")


def _splitParagraphs(text: str) -> list[str]:
    """按空行切段；无空行时整段作为一段。"""
    return [p.strip() for p in _BLANK_LINE_RE.split(text) if p.strip()]


def _parsePlainText(text: str) -> list[TextBlock]:
    """解析 MD/TXT：``#`` 标题开章节，标题本身保留为块。"""
    blocks: list[TextBlock] = []
    section: str | None = None
    paraNo = 0
    for para in _splitParagraphs(text):
        heading = _MD_HEADING_RE.match(para)
        if heading:
            section = heading.group(2)
        paraNo += 1
        blocks.append(
            TextBlock(
                text=para, page_number=None, section_name=section, paragraph_no=paraNo
            )
        )
    return blocks


def _parsePdf(content: bytes) -> list[TextBlock]:
    """解析 PDF：每页按空行切段，段号在页内重新计数。"""
    try:
        import io

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(content))
        blocks: list[TextBlock] = []
        for pageIdx, page in enumerate(reader.pages):
            pageText = page.extract_text() or ""
            paraNo = 0
            for para in _splitParagraphs(pageText):
                paraNo += 1
                blocks.append(
                    TextBlock(
                        text=para,
                        page_number=pageIdx + 1,
                        section_name=None,
                        paragraph_no=paraNo,
                    )
                )
        return blocks
    except Exception as e:
        raise DocumentParserError(f"PDF parsing failed: {e}") from e


def _isHeadingParagraph(para: object) -> bool:
    """判定段落是否为标题：直读 ``w:pStyle`` 的样式 ID。

    为什么不用 ``para.style.name``：后者要先解析文档的 ``word/styles.xml``
    部件，部件缺失或样式表引用不完整时 python-docx 会把**所有**段落一律报成
    ``Normal``，标题被静默漏掉（已实测）。``w:pStyle/@w:val`` 是样式 ID，
    任何合乎规范的 DOCX 都带，不受样式表能否解析影响。

    用 ``para._p`` 是有意为之：python-docx 没有公开 ``pStyle`` 的访问器，
    这是社区通行的下钻方式。

    已知限制：中文版 Word 用数字样式 ID（``1``/``2``/``3`` 对应标题 1/2/3），
    本实现只认 ``Heading<n>`` 形式，这类文档的 ``section_name`` 会退化为
    None（页码与段号不受影响）。该限制记入变更记录，不在 P0 处理。
    """
    from docx.oxml.ns import qn

    pPr = para._p.find(qn("w:pPr"))  # type: ignore[attr-defined]
    if pPr is None:
        return False
    pStyle = pPr.find(qn("w:pStyle"))
    if pStyle is None:
        return False
    styleId = pStyle.get(qn("w:val")) or ""
    return styleId.lower().startswith("heading")


def _parseDocx(content: bytes) -> list[TextBlock]:
    """解析 DOCX：标题段落开章节，段号在文档内递增。"""
    try:
        import io

        from docx import Document

        doc = Document(io.BytesIO(content))
        blocks: list[TextBlock] = []
        section: str | None = None
        paraNo = 0
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            if _isHeadingParagraph(para):
                section = text
            paraNo += 1
            blocks.append(
                TextBlock(
                    text=text,
                    page_number=None,
                    section_name=section,
                    paragraph_no=paraNo,
                )
            )
        return blocks
    except Exception as e:
        raise DocumentParserError(f"DOCX parsing failed: {e}") from e
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_document_parser.py -q
```

Expected: PASS（12 个用例）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/document_parser.py backend/app/tests/unit/test_document_parser.py
git commit -m "feat(wiki): document_parser 返回 TextBlock 保留页码/章节/段号"
```

---

## Task 2: `Chunk` 定位符与 `split_by_paragraphs` 签名变更

**Files:**
- Modify: `backend/app/services/chunk_splitter.py`（95 行 → 约 130 行）
- Test: `backend/app/tests/unit/test_chunk_splitter.py`

**Interfaces:**
- Consumes: `TextBlock`（Task 1）
- Produces:
  - `def split_by_paragraphs(blocks: list[TextBlock], chunk_size: int = 500, overlap: int = 50) -> list[Chunk]`
  - `Chunk.metadata` 现在是 `{"page_number": int|None, "section_name": str|None, "paragraph_no": int|None}`
  - `Chunk.to_dict()` 因为 `**self.metadata` 会自动摊平这三个键（**无需改动** `to_dict`）

> **陷阱（必须处理）**：原实现有**两个** `Chunk` 构造点 —— `_make_chunk`（第 95 行）与超长段落分支的**直接构造**（第 72 行）。只把定位符写进 `_make_chunk` 会漏掉超长段落那条路径。本任务把两处统一走 `_make_chunk`，并用测试钉死。
>
> **chunk 不得跨页**（2026-09-12 人类裁决修订，原「跨块取首块」方案作废）：累积块时若遇到
> **页码不同**的块，先 flush 再另起 —— 保证任一 chunk 的 `page_number` 对其**全部**正文都成立。
> 理由：P3 的 `evidence` 三个定位列直接吃这个值，跨页 chunk 被标成起点页等于证据链指错页，
> 而溯源正是这套东西的目的。
> **同一页内**跨块仍取首块定位符（段号取起点段，属可接受近似）。
> `page_number` 为 `None` 的格式（DOCX/MD）不做页边界切分，维持原累积逻辑。
> 需在 docstring 写明（docstring 里**不得**保留「取第一个块」的旧描述）。

- [ ] **Step 1: 写失败测试**

替换 `backend/app/tests/unit/test_chunk_splitter.py` 全文：

```python
"""chunk_splitter 单测：切分行为 + 定位符透传（P0 溯源地基）。"""

from __future__ import annotations

import pytest

from app.services.chunk_splitter import Chunk, split_by_paragraphs
from app.services.document_parser import TextBlock


def _block(text: str, *, page: int | None = None, section: str | None = None, para: int = 1) -> TextBlock:
    return TextBlock(text=text, page_number=page, section_name=section, paragraph_no=para)


class TestSplitBehavior:
    """保持既有切分语义不回归。"""

    def test_empty_blocks_returns_empty_list(self) -> None:
        assert split_by_paragraphs([]) == []

    def test_single_short_block(self) -> None:
        chunks = split_by_paragraphs([_block("这是一个短段落。")])
        assert len(chunks) == 1
        assert chunks[0].text == "这是一个短段落。"
        assert chunks[0].sequence == 0

    def test_multiple_blocks(self) -> None:
        blocks = [_block("第一段。", para=1), _block("第二段。", para=2), _block("第三段。", para=3)]
        chunks = split_by_paragraphs(blocks, chunk_size=5)
        assert len(chunks) == 3
        assert [c.text for c in chunks] == ["第一段。", "第二段。", "第三段。"]

    def test_chunk_size_respected(self) -> None:
        chunks = split_by_paragraphs([_block("A" * 1000)], chunk_size=500)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c.text) <= 500

    def test_overlap_between_chunks(self) -> None:
        chunks = split_by_paragraphs([_block("第一段内容。" * 100)], chunk_size=200, overlap=50)
        assert len(chunks) >= 2
        assert chunks[0].text[-50:] == chunks[1].text[:50]

    def test_chunk_has_id_and_sequence(self) -> None:
        chunks = split_by_paragraphs([_block("第一段。"), _block("第二段。")], chunk_size=5)
        assert chunks[0].chunk_id == "chunk-0"
        assert chunks[0].sequence == 0
        assert chunks[1].chunk_id == "chunk-1"
        assert chunks[1].sequence == 1


class TestLocators:
    """定位符必须一路带到 Chunk，含超长段落分支。"""

    def test_locator_carried_to_chunk(self) -> None:
        chunks = split_by_paragraphs([_block("正文", page=18, section="质量管理", para=3)])
        assert chunks[0].metadata["page_number"] == 18
        assert chunks[0].metadata["section_name"] == "质量管理"
        assert chunks[0].metadata["paragraph_no"] == 3

    def test_overlong_block_keeps_locator(self) -> None:
        """超长段落走的是另一条构造路径，定位符不能漏。"""
        chunks = split_by_paragraphs([_block("X" * 600, page=7, para=2)], chunk_size=200)
        assert len(chunks) >= 3
        for c in chunks:
            assert c.metadata["page_number"] == 7
            assert c.metadata["paragraph_no"] == 2

    def test_chunk_does_not_span_pages(self) -> None:
        # 跨页必须切开：否则 chunk 正文含第 2 页文字却被标成第 1 页。
        blocks = [_block("甲", page=1, para=1), _block("乙", page=2, para=1)]
        chunks = split_by_paragraphs(blocks, chunk_size=500)
        assert len(chunks) == 2
        assert [c.metadata["page_number"] for c in chunks] == [1, 2]
        assert "乙" not in chunks[0].text

    def test_same_page_blocks_still_merge(self) -> None:
        # 同页内跨块仍合并，取首块定位符（段号近似为起点段）。
        blocks = [_block("甲", page=1, para=1), _block("乙", page=1, para=2)]
        chunks = split_by_paragraphs(blocks, chunk_size=500)
        assert len(chunks) == 1
        assert chunks[0].metadata["page_number"] == 1
        assert chunks[0].metadata["paragraph_no"] == 1

    def test_page_none_blocks_still_merge(self) -> None:
        # DOCX/MD：page_number 为 None，不做页边界切分，维持原累积逻辑。
        blocks = [_block("甲", page=None, para=1), _block("乙", page=None, para=2)]
        chunks = split_by_paragraphs(blocks, chunk_size=500)
        assert len(chunks) == 1

    def test_to_dict_flattens_locators(self) -> None:
        chunks = split_by_paragraphs([_block("内容", page=5, para=9)])
        d = chunks[0].to_dict()
        assert d["chunk_id"] == "chunk-0"
        assert d["page_number"] == 5
        assert d["paragraph_no"] == 9

    def test_locator_none_values_preserved(self) -> None:
        chunks = split_by_paragraphs([_block("纯文本")])
        assert chunks[0].metadata["page_number"] is None
        assert chunks[0].metadata["section_name"] is None


class TestChunkToDict:
    def test_chunk_to_dict_basic(self) -> None:
        chunk = Chunk(chunk_id="chunk-0", text="测试文本", sequence=0)
        d = chunk.to_dict()
        assert d["chunk_id"] == "chunk-0"
        assert d["text"] == "测试文本"
        assert d["sequence"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_chunk_splitter.py -q
```

Expected: FAIL — `AttributeError: 'list' object has no attribute 'split'`（旧实现 `text.split("\n")` 收到列表）

- [ ] **Step 3: 实现**

替换 `backend/app/services/chunk_splitter.py` 全文：

```python
"""文档分块器：把 TextBlock 列表切成 Chunk，并携带定位符。

P0 溯源地基：``Chunk.metadata`` 原本已存在但从未被填充，是现成的扩展点。
本实现把 TextBlock 的页码/章节/段号写进 metadata，供下游写入 Milvus 并
最终支撑证据链的出处展示。
"""

from __future__ import annotations

from app.services.document_parser import TextBlock


class Chunk:
    """单个文本块。

    ``metadata`` 承载定位符（``page_number`` / ``section_name`` /
    ``paragraph_no``），``to_dict()`` 会把它摊平到顶层。
    """

    def __init__(
        self,
        chunk_id: str,
        text: str,
        sequence: int,
        metadata: dict | None = None,
    ) -> None:
        self.chunk_id = chunk_id
        self.text = text
        self.sequence = sequence
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "sequence": self.sequence,
            **self.metadata,
        }


def split_by_paragraphs(
    blocks: list[TextBlock],
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[Chunk]:
    """按段落切分文本块，块过长则再按固定长度切。

    跨块的 chunk 其定位符取**第一个块**（chunk 的起点）；跨页 chunk 的
    引用因此是近似的，但方向正确（指向起点页），优于完全不记。

    Args:
        blocks: 带定位信息的文本块（来自 parse_document）
        chunk_size: 每个 chunk 的最大字符数
        overlap: 相邻 chunk 之间的重叠字符数

    Returns:
        Chunk 列表（sequence 从 0 开始）
    """
    chunks: list[Chunk] = []
    seq = 0
    accumulated: list[TextBlock] = []
    accumulated_len = 0

    def flush() -> None:
        nonlocal seq, accumulated, accumulated_len
        if accumulated:
            chunks.append(_make_chunk(accumulated, seq))
            seq += 1
            accumulated = []
            accumulated_len = 0

    for block in blocks:
        blockLen = len(block.text)
        # 单块超过 chunk_size：先 flush 累积，再按固定长度切（带 overlap）
        if blockLen > chunk_size:
            flush()
            for start in range(0, blockLen, chunk_size - overlap):
                sub = block.text[start : start + chunk_size]
                chunks.append(_make_chunk([TextBlock(
                    text=sub,
                    page_number=block.page_number,
                    section_name=block.section_name,
                    paragraph_no=block.paragraph_no,
                )], seq))
                seq += 1
        elif accumulated_len + blockLen + (1 if accumulated else 0) <= chunk_size:
            if accumulated:
                accumulated_len += 1  # newline
            accumulated.append(block)
            accumulated_len += blockLen
        else:
            # 剩余空间不够放本块，flush 后另起
            flush()
            accumulated = [block]
            accumulated_len = blockLen

    flush()
    return chunks


def _make_chunk(blocks: list[TextBlock], seq: int) -> Chunk:
    """唯一的 Chunk 构造点：所有路径都必须经过它，定位符才不会漏。"""
    text = "\n".join(b.text for b in blocks)
    first = blocks[0]
    return Chunk(
        chunk_id=f"chunk-{seq}",
        text=text,
        sequence=seq,
        metadata={
            "page_number": first.page_number,
            "section_name": first.section_name,
            "paragraph_no": first.paragraph_no,
        },
    )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_chunk_splitter.py -q
```

Expected: PASS（12 个用例）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/chunk_splitter.py backend/app/tests/unit/test_chunk_splitter.py
git commit -m "refactor(wiki): split_by_paragraphs 吃 TextBlock 并把定位符写进 Chunk.metadata"
```

---

## Task 3: Milvus 文档集合加定位符字段 + 重建脚本

**Files:**
- Modify: `backend/app/infrastructure/milvus_client.py`
- Create: `backend/scripts/rebuild_document_collection.py`
- Test: `backend/app/tests/integration/test_milvus_document_fields.py`

**Interfaces:**
- Consumes: `Chunk.metadata`（Task 2）
- Produces:
  - `_documentFields()` 新增 3 个字段：`page_number` (INT64)、`section_name` (VARCHAR 200)、`paragraph_no` (INT64)
  - `insertDocumentChunks(records)` 接受并写入 `page_number` / `section_name` / `paragraph_no`
  - `searchDocumentChunks(..., output_fields=...)` 默认输出这 3 个字段
  - `deleteDocumentChunks(documentId: str) -> None` —— 按 `document_id` 删除该文档的全部 chunk（供 Task 7 的门禁脚本自清，避免门禁每跑一次就给共享集合留一份垃圾）
  - `queryDocumentChunks(documentId: str) -> list[dict]` —— 按 `document_id` 直查该文档的 chunk（不经向量检索），供 Task 7 校验定位符

> **Milvus 标量字段不可为 NULL**（2.4.x 无 nullable 标量）。空值用哨兵：`page_number` / `paragraph_no` 用 `-1`，`section_name` 用 `""`。这是无损的——`-1` 不是合法页码/段号。
>
> **`_ensureCollection` 已存在即早返回**（`milvus_client.py:104-113`），所以**只改字段不重建不生效**。且 `CollectionSchema` **未设 `enable_dynamic_field`**（默认 False），无法靠动态字段绕过。**执行前必须再验集合行数**（宿主机即可：`cd backend && .venv/bin/python` 连 `MILVUS_URI=http://localhost:19530`）。
>
> ⚠️ **实测结果推翻了「集合为空」这一前提。** 2026-09-12 执行 Task 3 时实测：集合有
> **333 行 / 299 个文档**，且是旧 8 字段 schema（无任何定位符字段）。同刻 prod
> `document_catalog` 是 **0 行**，所以这 333 行**没有任何目录引用，属孤儿数据**
> （成分：297 个单 `chunk-0` 文档、1 行 `DOC-VERIFY-SMOKE`、2 个各 18 chunk 的文档，
> 后者内容是真实的供应商能力标准正文）。
>
> **处置决定（用户 2026-09-12）：先导快照，再重建。** 快照已由控制器执行并校验：
>
> ```
> backups/milvus/document_embeddings_20260912_1953.json.gz
> 333 行 / 7 个标量字段 + 1024 维 embedding / 999771 字节
> sha256 b66d6c849530b4b78fad44e01341ebb1f75e393c7c676e11ba2806e8cbe501e8
> ```
>
> 于是 Task 3 Step 7 的重建以显式标志执行：
>
> ```bash
> cd backend
> ALLOW_NONEMPTY_REBUILD=1 .venv/bin/python scripts/rebuild_document_collection.py
> ```
>
> 重建后**必须回读 schema 字段核对**（`Collection(...).schema.fields`），不能拿脚本打印的
> 「已重建集合」当证据 —— 这个闸门存在的全部意义就是「命令跑成功」不等于「schema 是对的」。

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/integration/test_milvus_document_fields.py`：

```python
"""Milvus document_embeddings 定位符字段（P0 溯源地基）。

真实 Milvus + 真实集合，不 mock。
"""

from __future__ import annotations

import pytest

from app.infrastructure.milvus_client import (
    _documentFields,
    deleteDocumentChunks,
    ensureDocumentCollection,
    insertDocumentChunks,
    queryDocumentChunks,
    searchDocumentChunks,
)


class TestDocumentFields:
    def test_locator_fields_present(self) -> None:
        names = [f.name for f in _documentFields()]
        assert "page_number" in names
        assert "section_name" in names
        assert "paragraph_no" in names

    def test_field_order_matches_insert_payload(self) -> None:
        """insertDocumentChunks 用位置列表写数据，字段顺序即契约。"""
        names = [f.name for f in _documentFields()]
        assert names[-1] == "embedding", "embedding 必须最后，与 data 列表一致"
        assert names.index("page_number") < names.index("embedding")


class TestRoundTrip:
    @pytest.mark.integration
    def test_locator_survives_insert_and_search(self) -> None:
        ensureDocumentCollection()
        insertDocumentChunks(
            [
                {
                    "document_id": "DOC-P0-TEST",
                    "chunk_id": "chunk-loc",
                    "chunk_text": "供应商A暂停采购",
                    "chunk_sequence": 0,
                    "effective_date": "",
                    "security_level": "L1",
                    "page_number": 18,
                    "section_name": "质量管理",
                    "paragraph_no": 3,
                    "embedding": [0.1] * 1024,
                }
            ]
        )
        hit = _findChunk("chunk-loc", [0.1] * 1024)
        assert hit is not None, "应能检索到刚写入的 chunk"
        assert hit["page_number"] == 18
        assert hit["section_name"] == "质量管理"
        assert hit["paragraph_no"] == 3

    @pytest.mark.integration
    def test_missing_locator_writes_sentinel(self) -> None:
        ensureDocumentCollection()
        insertDocumentChunks(
            [
                {
                    "document_id": "DOC-P0-TEST",
                    "chunk_id": "chunk-null",
                    "chunk_text": "无定位符",
                    "chunk_sequence": 1,
                    "page_number": None,
                    "section_name": None,
                    "paragraph_no": None,
                    "embedding": [0.2] * 1024,
                }
            ]
        )
        hit = _findChunk("chunk-null", [0.2] * 1024)
        assert hit is not None
        assert hit["page_number"] == -1
        assert hit["section_name"] == ""
        assert hit["paragraph_no"] == -1


    @pytest.mark.integration
    def test_delete_document_chunks_scopes_to_one_document(self) -> None:
        """按 document_id 删除只带走该文档的 chunk，不是清空集合。"""
        ensureDocumentCollection()
        insertDocumentChunks(
            [
                {
                    "document_id": "DOC-P0-DEL",
                    "chunk_id": "chunk-del-a",
                    "chunk_text": "待删除",
                    "chunk_sequence": 0,
                    "page_number": 1,
                    "section_name": "",
                    "paragraph_no": 1,
                    "embedding": [0.3] * 1024,
                },
                {
                    "document_id": "DOC-P0-KEEP",
                    "chunk_id": "chunk-del-b",
                    "chunk_text": "不该被删",
                    "chunk_sequence": 0,
                    "page_number": 1,
                    "section_name": "",
                    "paragraph_no": 1,
                    "embedding": [0.4] * 1024,
                },
            ]
        )

        deleteDocumentChunks("DOC-P0-DEL")

        assert _findChunk("chunk-del-a", [0.3] * 1024) is None
        assert _findChunk("chunk-del-b", [0.4] * 1024) is not None

        # 收尾：本用例同样不该给共享集合留残留
        deleteDocumentChunks("DOC-P0-KEEP")

    @pytest.mark.integration
    def test_query_document_chunks_returns_locators(self) -> None:
        """按 document_id 直查（不经向量检索）也要能拿到定位符。"""
        ensureDocumentCollection()
        insertDocumentChunks(
            [
                {
                    "document_id": "DOC-P0-QRY",
                    "chunk_id": "chunk-qry",
                    "chunk_text": "直查定位符",
                    "chunk_sequence": 0,
                    "page_number": 7,
                    "section_name": "采购管理",
                    "paragraph_no": 2,
                    "embedding": [0.5] * 1024,
                }
            ]
        )

        rows = [r for r in queryDocumentChunks("DOC-P0-QRY") if r["chunk_id"] == "chunk-qry"]
        assert len(rows) == 1
        assert rows[0]["page_number"] == 7
        assert rows[0]["section_name"] == "采购管理"
        assert rows[0]["paragraph_no"] == 2

        deleteDocumentChunks("DOC-P0-QRY")


def _findChunk(chunkId: str, embedding: list[float]) -> dict | None:
    """按 chunk_id 在检索结果里定位，避免依赖排序位置。

    集合是跨用例共享的，topK=1 取到的未必是本用例刚写的那条。
    """
    for hit in searchDocumentChunks(embedding, topK=10):
        if hit["chunk_id"] == chunkId:
            return hit
    return None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/integration/test_milvus_document_fields.py -q
```

Expected: FAIL — `AssertionError: assert 'page_number' in ['id', 'document_id', ...]`

- [ ] **Step 3: 实现 —— 改 `_documentFields()`**

`backend/app/infrastructure/milvus_client.py`，替换 `_documentFields`：

```python
def _documentFields() -> list[FieldSchema]:
    """document_embeddings 的字段定义。

    ⚠️ 顺序即契约：``insertDocumentChunks`` 用位置列表写数据，增删字段必须
    同时改这里与那里的 data 列表。``id`` 是 auto_id 主键，不出现在 data 中。
    ⚠️ Milvus 2.4 标量字段不支持 NULL，空值用哨兵：页码/段号 -1，章节 ""。
    """
    return [
        FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=50),
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="chunk_text", dtype=DataType.VARCHAR, max_length=4000),
        FieldSchema(name="chunk_sequence", dtype=DataType.INT64),
        FieldSchema(name="effective_date", dtype=DataType.VARCHAR, max_length=20),
        FieldSchema(name="security_level", dtype=DataType.VARCHAR, max_length=10),
        FieldSchema(name="page_number", dtype=DataType.INT64),
        FieldSchema(name="section_name", dtype=DataType.VARCHAR, max_length=200),
        FieldSchema(name="paragraph_no", dtype=DataType.INT64),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=_DIM),
    ]
```

- [ ] **Step 4: 实现 —— 改 `insertDocumentChunks`**

替换同文件中的 `insertDocumentChunks`：

```python
def _locatorInt(value: Any) -> int:
    """Milvus 标量不可为 NULL，缺失定位符写哨兵 -1（非合法页码/段号）。"""
    return int(value) if value is not None else -1


def insertDocumentChunks(records: list[dict[str, Any]]) -> None:
    """批量插入文档 chunk 向量记录。

    Args:
        records: 每条记录含 document_id, chunk_id, chunk_text, chunk_sequence,
                 effective_date, security_level, page_number, section_name,
                 paragraph_no, embedding。

                定位符为 None 时写哨兵（页码/段号 -1，章节 ""）。
                顺序必须与 ``_documentFields()`` 一致（id 除外）。
    """
    collection = ensureDocumentCollection()
    data = [
        [r["document_id"] for r in records],
        [r["chunk_id"] for r in records],
        [r["chunk_text"][:4000] for r in records],  # truncate to max_length
        [r["chunk_sequence"] for r in records],
        [r.get("effective_date") or "" for r in records],
        [r.get("security_level") or "" for r in records],
        [_locatorInt(r.get("page_number")) for r in records],
        [(r.get("section_name") or "")[:200] for r in records],
        [_locatorInt(r.get("paragraph_no")) for r in records],
        [r["embedding"] for r in records],
    ]
    collection.insert(data)
    collection.flush()
    logger.info("Inserted %d document chunks into Milvus", len(records))
```

- [ ] **Step 5: 实现 —— `searchDocumentChunks` 输出定位符**

该函数的 `output_fields` 是**硬编码在 `collection.search(...)` 调用里的**（不是参数），返回字典也是固定形状，所以两处都要改。

`backend/app/infrastructure/milvus_client.py`，把 `searchDocumentChunks` 里的检索与结果组装改为：

```python
    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param={"metric_type": "L2", "params": {"n_probe": 10}},
        limit=topK,
        output_fields=[
            "document_id",
            "chunk_id",
            "chunk_text",
            "chunk_sequence",
            "security_level",
            "page_number",
            "section_name",
            "paragraph_no",
        ],
        expr=expr,
    )

    hits: list[dict[str, Any]] = []
    for result in results:
        for hit in result:
            hits.append({
                "document_id": hit.entity.get("document_id"),
                "chunk_id": hit.entity.get("chunk_id"),
                "chunk_text": hit.entity.get("chunk_text"),
                "chunk_sequence": hit.entity.get("chunk_sequence"),
                "security_level": hit.entity.get("security_level"),
                "page_number": hit.entity.get("page_number"),
                "section_name": hit.entity.get("section_name"),
                "paragraph_no": hit.entity.get("paragraph_no"),
                "distance": float(hit.distance),
            })
    return hits
```

同时把 docstring 的 Returns 行补上这三个键：

```
        匹配的 chunk 列表，含 document_id, chunk_id, chunk_text, chunk_sequence,
        security_level, page_number, section_name, paragraph_no, distance

        注意定位符是**哨兵值**：无页码/段号时为 -1，无章节时为 ""（Milvus 2.4
        标量字段不支持 NULL），消费方需自行判断，不要直接展示 -1。
```

同一文件里再加一个按文档删除的辅助函数（Task 7 的门禁脚本要用它自清）：

```python
def deleteDocumentChunks(documentId: str) -> None:
    """删除指定 document_id 的全部 chunk。

    表达式**必须**带 document_id 过滤：集合是跨调用方共享的，一个没有
    过滤条件的 ``collection.delete("")`` 会把整个集合清空。
    """
    collection = ensureDocumentCollection()
    collection.delete(f'document_id == "{documentId}"')
    collection.flush()
    logger.info("Deleted Milvus document chunks for document_id=%s", documentId)
```

还要一个**按 document_id 直查**的函数（Task 7 的门禁用它读回定位符）：

```python
def queryDocumentChunks(documentId: str) -> list[dict[str, Any]]:
    """按 document_id 查出该文档的全部 chunk（不走向量检索）。

    门禁脚本要检查的是「写进去的定位符对不对」，不是「检索得准不准」。
    用 ``searchDocumentChunks`` 会因为集合跨调用方共享、topK 截断而漏掉
    目标行 —— 那会把门禁变成抛硬币。
    """
    collection = ensureDocumentCollection()
    collection.load()
    return collection.query(
        expr=f'document_id == "{documentId}"',
        output_fields=[
            "document_id",
            "chunk_id",
            "chunk_text",
            "chunk_sequence",
            "page_number",
            "section_name",
            "paragraph_no",
        ],
        limit=16384,
    )
```

- [ ] **Step 6: 建重建脚本**

新建 `backend/scripts/rebuild_document_collection.py`：

```python
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
```

- [ ] **Step 7: 执行重建并跑测试**

```bash
cd backend
.venv/bin/python scripts/rebuild_document_collection.py
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/integration/test_milvus_document_fields.py -q
```

> **为什么在宿主机跑**：重建必须由**新代码**执行，否则按旧字段重建且静默成功。
> 容器里的 `app` 包要到 Task 7 重建镜像后才更新；而且 Task 3 不做部署，
> `/app/scripts/rebuild_document_collection.py` 此刻在容器里根本不存在。
> 宿主机这条路是现成的：`backend/.env` 里 `MILVUS_URI=http://localhost:19530`
> （Milvus 端口已映射），且项目以 editable 方式装进了 `.venv`（hatchling
> `packages = ["app"]`），`scripts/` 下的既有脚本同样直接 `from app...` 导入。

Expected: 重建脚本输出「已重建集合」；测试 PASS（6 个用例）

- [ ] **Step 8: 提交**

```bash
git add backend/app/infrastructure/milvus_client.py backend/scripts/rebuild_document_collection.py \
        backend/app/tests/integration/test_milvus_document_fields.py
git commit -m "feat(wiki): Milvus 文档集合加定位符字段 + 集合重建脚本"
```

---

## Task 4: 对象存储客户端与 minio 依赖

**Files:**
- Create: `backend/app/infrastructure/object_storage.py`
- Create: `scripts/backup_objects.sh`（**仓库根**的 `scripts/`，与 `backup_pg.sh` / `deploy_backend.sh` 同级；
  脚本内 `BACKUP_DIR` 由 `dirname "$0"/..` 推出 `<repo>/backups/objects`，放 `backend/scripts/` 会落到 `backend/backups/`）
- Modify: `backend/pyproject.toml`、`backend/uv.lock`
- Modify: `docker/docker-compose.yml`
- Modify: `Harness/rules/数据存储防护.md`
- Test: `backend/app/tests/unit/test_object_storage.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class ObjectStorageError(Exception)`
  - `def hashContent(content: bytes) -> str` —— sha256 十六进制
  - `def buildSourceObjectName(contentHash: str, filename: str) -> str` —— `sources/<hash[:2]>/<hash>/<safeName>`
  - `def ensureBucket(bucket: str = DEFAULT_BUCKET) -> None`
  - `def putSourceObject(objectName: str, content: bytes, contentType: str, bucket: str = DEFAULT_BUCKET) -> str` —— 返回 `s3://<bucket>/<objectName>`
  - `def getSourceObject(objectName: str, bucket: str = DEFAULT_BUCKET) -> bytes`
  - `def _getClient()` —— 可被测试 monkeypatch

> **内容寻址**：对象名由内容哈希派生，同一文件重复上传落到同一个 key，天然去重，与 P1 的 `page_id` 内容派生同一思路。
>
> **配置缺失必须显式失败**：`MINIO_ENDPOINT` / `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 任一缺失即抛 `ObjectStorageError`，**不降级、不静默跳过**。
>
> **不复用 `qa-milvus-minio`**：那是 Milvus 私有后端（不发布端口、硬编码 `minioadmin/minioadmin`）。

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/unit/test_object_storage.py`：

```python
"""对象存储客户端单测（P0 溯源地基）。MinIO 客户端被 mock。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.infrastructure.object_storage import (
    ObjectStorageError,
    buildSourceObjectName,
    hashContent,
    putSourceObject,
)


class TestHashContent:
    def test_sha256_known_value(self) -> None:
        # echo -n "" | sha256sum
        assert hashContent(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_same_content_same_hash(self) -> None:
        assert hashContent(b"abc") == hashContent(b"abc")

    def test_different_content_different_hash(self) -> None:
        assert hashContent(b"abc") != hashContent(b"abd")


class TestBuildSourceObjectName:
    def test_content_addressed_layout(self) -> None:
        h = hashContent(b"abc")
        name = buildSourceObjectName(h, "制度.pdf")
        assert name == f"sources/{h[:2]}/{h}/制度.pdf"

    def test_same_content_same_name_regardless_of_filename(self) -> None:
        h = hashContent(b"abc")
        assert buildSourceObjectName(h, "a.pdf") != buildSourceObjectName(h, "b.pdf")

    def test_path_traversal_in_filename_is_neutralized(self) -> None:
        h = hashContent(b"abc")
        name = buildSourceObjectName(h, "../../etc/passwd")
        assert ".." not in name
        assert name.startswith(f"sources/{h[:2]}/{h}/")


class TestPutSourceObject:
    def test_missing_config_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ObjectStorageError, match="MINIO_ENDPOINT"):
                putSourceObject("sources/aa/x/a.pdf", b"data", "application/pdf")

    def test_returns_s3_url(self) -> None:
        fake = MagicMock()
        with patch.dict(
            "os.environ",
            {
                "MINIO_ENDPOINT": "objects:9000",
                "MINIO_ROOT_USER": "u",
                "MINIO_ROOT_PASSWORD": "p",
            },
        ):
            with patch(
                "app.infrastructure.object_storage._getClient", return_value=fake
            ):
                url = putSourceObject("sources/aa/x/a.pdf", b"data", "application/pdf")
        assert url == "s3://qa-knowledge-sources/sources/aa/x/a.pdf"
        fake.put_object.assert_called_once()

    def test_client_error_wrapped_as_object_storage_error(self) -> None:
        fake = MagicMock()
        fake.put_object.side_effect = RuntimeError("connection refused")
        with patch.dict(
            "os.environ",
            {
                "MINIO_ENDPOINT": "objects:9000",
                "MINIO_ROOT_USER": "u",
                "MINIO_ROOT_PASSWORD": "p",
            },
        ):
            with patch(
                "app.infrastructure.object_storage._getClient", return_value=fake
            ):
                with pytest.raises(ObjectStorageError, match="connection refused"):
                    putSourceObject("sources/aa/x/a.pdf", b"data", "application/pdf")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_object_storage.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.infrastructure.object_storage'`

- [ ] **Step 3: 实现**

新建 `backend/app/infrastructure/object_storage.py`：

```python
"""对象存储（MinIO）：知识源文件留存。

P0 溯源地基的一部分。此前源文件上传后不留底，``document_catalog.storage_url``
写的是 ``milvus://N_chunks`` 这种假 URL，``content_hash`` 恒为 None。本模块
提供真实的对象存储读写，使证据链能回指原始文件。

对象名内容寻址（``sources/<hash[:2]>/<hash>/<name>``）：同一文件重复上传落到
同一 key，天然去重。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = "qa-knowledge-sources"

# 文件名里可能带路径分隔符或 ..，落到对象名上会越权，统一净化。
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._一-鿿-]")


class ObjectStorageError(Exception):
    """对象存储操作失败。

    与「对象不存在」区分：前者是基础设施故障（调用方应上报并中止），
    后者是正常的查询未命中。
    """
    pass


def hashContent(content: bytes) -> str:
    """内容 SHA-256（十六进制小写）。"""
    return hashlib.sha256(content).hexdigest()


def buildSourceObjectName(contentHash: str, filename: str) -> str:
    """由内容哈希 + 文件名派生对象名（内容寻址）。"""
    safeName = _UNSAFE_NAME_RE.sub("_", filename.rsplit("/", 1)[-1]).strip("_")
    if not safeName:
        safeName = "unnamed"
    return f"sources/{contentHash[:2]}/{contentHash}/{safeName}"


def _getClient() -> Any:
    """构造 MinIO 客户端。配置缺失即失败，不降级。

    先校验配置，再 import SDK：配置缺失是本模块**自己的**错误，不该被
    SDK 未安装的 ``ModuleNotFoundError`` 抢在前面掩盖掉 —— 那会把
    「没配 key」误报成「依赖没装」，排查方向完全错。
    """
    endpoint = os.environ.get("MINIO_ENDPOINT")
    accessKey = os.environ.get("MINIO_ROOT_USER")
    secretKey = os.environ.get("MINIO_ROOT_PASSWORD")
    if not endpoint:
        raise ObjectStorageError("MINIO_ENDPOINT 未配置")
    if not accessKey:
        raise ObjectStorageError("MINIO_ROOT_USER 未配置")
    if not secretKey:
        raise ObjectStorageError("MINIO_ROOT_PASSWORD 未配置")

    from minio import Minio

    return Minio(endpoint, access_key=accessKey, secret_key=secretKey, secure=False)


def ensureBucket(bucket: str = DEFAULT_BUCKET) -> None:
    """幂等创建桶。"""
    client = _getClient()
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logger.info("Created object storage bucket '%s'", bucket)
    except ObjectStorageError:
        raise
    except Exception as e:
        raise ObjectStorageError(f"确保桶 {bucket} 存在失败: {e}") from e


def putSourceObject(
    objectName: str,
    content: bytes,
    contentType: str,
    bucket: str = DEFAULT_BUCKET,
) -> str:
    """存入源文件，返回 ``s3://<bucket>/<objectName>``。

    Raises:
        ObjectStorageError: 配置缺失或写入失败
    """
    import io

    client = _getClient()
    try:
        ensureBucket(bucket)
        client.put_object(
            bucket,
            objectName,
            io.BytesIO(content),
            length=len(content),
            content_type=contentType or "application/octet-stream",
        )
    except ObjectStorageError:
        raise
    except Exception as e:
        raise ObjectStorageError(f"源文件写入失败 {objectName}: {e}") from e
    return f"s3://{bucket}/{objectName}"


def getSourceObject(objectName: str, bucket: str = DEFAULT_BUCKET) -> bytes:
    """读回源文件。

    Raises:
        ObjectStorageError: 配置缺失、对象不存在或读取失败
    """
    client = _getClient()
    response = None
    try:
        response = client.get_object(bucket, objectName)
        return response.read()
    except ObjectStorageError:
        raise
    except Exception as e:
        raise ObjectStorageError(f"源文件读取失败 {objectName}: {e}") from e
    finally:
        if response is not None:
            response.close()
            response.release_conn()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_object_storage.py -q
```

Expected: PASS（9 个用例）

> 这 9 条单测**不需要 `minio` 已安装**：8 条不触碰 `_getClient`，第 9 条（`test_missing_config_raises`）
> 走的是 `_getClient` 的配置校验分支，而该函数**先校验配置、后 import SDK**。所以 Step 4
> 在加依赖（Step 5）之前就能全绿 —— `minio` 依赖是给真实数据脚本（Task 7）和运行时用的，
> 不是给单测用的。（已实测：既无 `minio` 也全绿。）

- [ ] **Step 5: 加依赖**

```bash
cd backend && uv add "minio>=7.2.0"
git add backend/pyproject.toml backend/uv.lock
```

> 若 `uv add` 因网络失败，手工在 `backend/pyproject.toml` 的 `dependencies` 列表按字母序插入 `"minio>=7.2.0",`，再跑 `cd backend && uv lock`。

- [ ] **Step 6: compose 加服务**

在 `docker/docker-compose.yml` 追加（并确认 `volumes:` 段已声明 `objects_data`）：

```yaml
  qa-objects:
    image: minio/minio:RELEASE.2024-06-13T22-53-53Z
    container_name: qa-objects
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER:?MINIO_ROOT_USER 必须显式配置}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD 必须显式配置}
    ports:
      - "9000:9000"
      - "9001:9001"
    volumes:
      - objects_data:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 30s
      timeout: 10s
      retries: 3
```

`volumes:` 段加：

```yaml
  objects_data:
```

并把三个 `MINIO_*` 变量（含端点）加到 `qa-backend` 的 `environment`：

```yaml
      MINIO_ENDPOINT: qa-objects:9000
      MINIO_ROOT_USER: ${MINIO_ROOT_USER:?MINIO_ROOT_USER 必须显式配置}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD 必须显式配置}
```

> **注意**：端口 9000/9001 在宿主机是空的——`qa-milvus-minio` 不发布任何端口（已验证）。
> **注意**：宿主机 `.env` 必须提供 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`，compose 的 `${VAR:?}` 会在缺失时直接拒绝启动，这是有意的（避免默认弱口令）。
>
> **这一步必须真做，不是提醒而已。** `docker/.env` 当前**一个 `MINIO_*` 变量都没有**
> （已核实），不补上，Task 7 的 `docker compose up -d qa-objects backend` 会直接拒绝启动。
> 先确认确实没有：
>
> ```bash
> grep -in minio docker/.env    # 预期：无输出
> ```
>
> 再用强口令追加（`docker/.env` 已被 `.gitignore` 忽略，**不要提交它**）：
>
> ```bash
> printf '\n# 知识源文件对象存储（qa-objects）。与 milvus-minio 的 minioadmin 无关，勿复用。\nMINIO_ROOT_USER=qa_objects\nMINIO_ROOT_PASSWORD=%s\n' "$(openssl rand -base64 24)" >> docker/.env
> ```
>
> 不要把这两个值以字面量写进 `docker-compose.yml` —— `${VAR:?}` 的意义正是
> 「缺失就启动失败」，而不是回退到一个默认弱口令。

- [ ] **Step 7: 备份脚本**

新建 `scripts/backup_objects.sh`：

```bash
#!/usr/bin/env bash
# 备份 MinIO 源文件卷（qa-knowledge-sources）。
#
# ⚠️ 与 backup_pg.sh 一样是**手动**脚本：本机 PG 的备份 cron 已确认静默失效
# （launchd 契约断裂），用户 2026-09-12 决定维持手动。此缺口记录在
# Harness/changes/feat-wiki-provenance/summary.md 第 9 段。
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$(cd "$(dirname "$0")/.." && pwd)/backups/objects}"
STAMP="$(date +%Y-%m-%d_%H%M)"
mkdir -p "$BACKUP_DIR"

docker run --rm \
  -v qa-system_objects_data:/data:ro \
  -v "$BACKUP_DIR":/backup \
  alpine:3.20 \
  tar czf "/backup/objects_data_${STAMP}.tar.gz" -C /data .

echo "已备份到 $BACKUP_DIR/objects_data_${STAMP}.tar.gz"
```

```bash
chmod +x scripts/backup_objects.sh
```

- [ ] **Step 8: 更新防护规则**

在 `Harness/rules/数据存储防护.md` 的卷清单表中新增一行：

```markdown
| `objects_data` | `qa-system_objects_data` | **知识源文件原件**，MinIO 后端。丢弃后 `document_catalog.storage_url` 全部失效，证据链断头 —— 不可重建（源文件只在用户手里） |
```

- [ ] **Step 9: 提交**

```bash
git add backend/app/infrastructure/object_storage.py backend/app/tests/unit/test_object_storage.py \
        backend/pyproject.toml backend/uv.lock docker/docker-compose.yml \
        scripts/backup_objects.sh Harness/rules/数据存储防护.md
git commit -m "feat(wiki): 独立 MinIO 对象存储留存知识源文件 + 备份脚本"
```

---

## Task 5: `rag_service` 写真实 `storage_url`/`content_hash`，消除静默失败

**Files:**
- Modify: `backend/app/services/rag_service.py:94-180`
- Test: `backend/app/tests/unit/test_rag_service.py`
- Create: `backend/app/tests/integration/test_rag_ingest_provenance.py`
- Modify: `backend/app/tests/integration/conftest.py`（上移 `mockEmbeddingService`）
- Modify: `backend/app/tests/integration/test_rag_api.py`（改为共用 conftest fixture）

**Interfaces:**
- Consumes: `TextBlock` / `split_by_paragraphs(list[TextBlock])`（Task 1/2）、`milvus insertDocumentChunks` 新字段（Task 3）、`object_storage`（Task 4）
- Produces: `RagService.ingestDocument(...)` 行为变更 —— 入库前先把源文件写入对象存储；`document_catalog` 记真实 url 与 hash

> **顺序调整**：源文件写入 MinIO 必须发生在 **Milvus 写入之前**。原实现在 Milvus 成功后才更新 catalog，且失败被 `except: pass` 吞掉。新顺序让 MinIO 故障在污染 Milvus 之前就显式失败。
>
> **行为变更（有意）**：catalog 更新分支的 `except Exception: pass` 被移除。理由见 spec §4.5「MinIO 不可用必须显式失败，不得降级为静默跳过」。这会改变一条既有测试的期望，属预期。

- [ ] **Step 1: 改测试**

修改 `backend/app/tests/unit/test_rag_service.py`。找到 patch `parse_document` 返回字符串的两处（约 `:229-237` 与 `:308-316`），把 mock 改成返回 `TextBlock` 列表，并给 `split_by_paragraphs` 的 mock chunk 补 `metadata`：

```python
        mock_blocks = [
            TextBlock(text="这是测试文档内容。", page_number=1, section_name=None, paragraph_no=1)
        ]
        mock_chunks = [
            MagicMock(
                chunk_id="chunk-0",
                text="这是测试文档内容。",
                sequence=0,
                metadata={"page_number": 1, "section_name": None, "paragraph_no": 1},
            ),
        ]

        with patch(
            "app.services.rag_service.parse_document",
            new_callable=AsyncMock,
            return_value=mock_blocks,
        ):
            with patch(
                "app.services.rag_service.split_by_paragraphs",
                return_value=mock_chunks,
            ):
                with patch(
                    "app.services.rag_service.putSourceObject",
                    return_value="s3://qa-knowledge-sources/sources/ab/abc/test.txt",
                ):
```

文件顶部补 import：

```python
from app.services.document_parser import TextBlock
```

再加三条新用例。**注意裸 `MagicMock` 的 `.metadata.get(...)` 会返回一个 MagicMock**，直接写进 Milvus records 语义不明，所以用下面的 `_mockChunk` 显式给定 metadata：

```python
def _mockChunk(
    chunkId: str,
    text: str,
    seq: int,
    *,
    page: int | None = 1,
    section: str | None = None,
    para: int | None = None,
) -> MagicMock:
    """构造带定位符 metadata 的假 chunk。

    `section` / `para` 必须由调用方显式传入。给个由 `seq` 推导的默认值看似
    方便，但要断言定位符透传的用例一旦依赖它，测的就成了「mock 的默认值
    进了 record」——同义反复，删掉实现里的透传也照样绿。
    """
    return MagicMock(
        chunk_id=chunkId,
        text=text,
        sequence=seq,
        metadata={
            "page_number": page,
            "section_name": section,
            "paragraph_no": para if para is not None else seq + 1,
        },
    )
```

```python
    @pytest.mark.asyncio
    async def test_ingest_stores_source_object_and_real_metadata(self) -> None:
        """源文件必须真存，catalog 记真实 url + hash，不再写假 milvus:// URL。"""
        # Arrange
        mock_session = MagicMock()
        mock_emb = AsyncMock()
        mock_emb.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)
        mock_blocks = [
            TextBlock(
                text="这是测试文档内容。", page_number=18, section_name="质量管理", paragraph_no=3
            )
        ]
        mock_chunks = [
            _mockChunk("chunk-0", "这是测试文档内容。", 0, page=18, section="质量管理", para=3)
        ]

        # Act
        with patch(
            "app.services.rag_service.parse_document",
            new_callable=AsyncMock,
            return_value=mock_blocks,
        ), patch(
            "app.services.rag_service.split_by_paragraphs", return_value=mock_chunks
        ), patch(
            "app.services.rag_service._getEmbeddingService", return_value=mock_emb
        ), patch(
            "app.services.rag_service.putSourceObject",
            return_value="s3://qa-knowledge-sources/sources/ab/abcd/test.txt",
        ), patch(
            "app.services.rag_service.insertDocumentChunks"
        ) as mock_insert:
            svc = RagService()
            svc._doc_svc = self._mock_doc_svc_with_create()
            result = await svc.ingestDocument(
                mock_session,
                content=b"dummy",
                filename="test.txt",
                mime_type="text/plain",
                actor=CurrentUser(userId="test-user"),
            )

        # Assert：返回值带真实 url + hash
        assert result["storage_url"].startswith("s3://qa-knowledge-sources/")
        assert result["content_hash"] is not None
        assert len(result["content_hash"]) == 64

        # Assert：落库的 document 元数据是真值
        # `createDocument(session, dto, actor=...)` 是位置传参，dto 在 args[1]。
        # 写成 kwargs["dto"] 会 KeyError —— 与下方 updateDocument 的坑同源。
        createdDto = svc._doc_svc.createDocument.await_args.args[1]
        assert createdDto.storage_url == "s3://qa-knowledge-sources/sources/ab/abcd/test.txt"
        assert createdDto.content_hash == result["content_hash"]

        # Assert：定位符进了 Milvus 记录
        record = mock_insert.call_args.args[0][0]
        assert record["page_number"] == 18
        assert record["section_name"] == "质量管理"
        assert record["paragraph_no"] == 3

    @pytest.mark.asyncio
    async def test_ingest_fails_loud_when_object_storage_fails(self) -> None:
        """MinIO 故障必须显式失败，不得静默降级，也不得先污染 Milvus。"""
        # Arrange
        mock_session = MagicMock()

        # Act / Assert
        with patch(
            "app.services.rag_service.parse_document",
            new_callable=AsyncMock,
            return_value=[TextBlock(text="内容", page_number=1, section_name=None, paragraph_no=1)],
        ), patch(
            "app.services.rag_service.putSourceObject",
            side_effect=ObjectStorageError("connection refused"),
        ), patch(
            "app.services.rag_service.insertDocumentChunks"
        ) as mock_insert:
            svc = RagService()
            svc._doc_svc = self._mock_doc_svc_with_create()
            with pytest.raises(RagError, match="源文件存储失败"):
                await svc.ingestDocument(
                    mock_session,
                    content=b"test",
                    filename="test.txt",
                    mime_type="text/plain",
                    actor=CurrentUser(userId="test-user"),
                )

        # 源文件存不下就不该往 Milvus 写
        mock_insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_existing_doc_rewrites_real_metadata(self) -> None:
        """已存在文档的更新分支也必须写真实值（原实现写的是 milvus://N_chunks 假 URL）。"""
        # Arrange
        mock_session = MagicMock()
        mock_emb = AsyncMock()
        mock_emb.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)
        existing = MagicMock(id=7)

        # Act
        with patch(
            "app.services.rag_service.parse_document",
            new_callable=AsyncMock,
            return_value=[TextBlock(text="内容", page_number=1, section_name=None, paragraph_no=1)],
        ), patch(
            "app.services.rag_service.split_by_paragraphs",
            return_value=[_mockChunk("chunk-0", "内容", 0)],
        ), patch(
            "app.services.rag_service._getEmbeddingService", return_value=mock_emb
        ), patch(
            "app.services.rag_service.putSourceObject",
            return_value="s3://qa-knowledge-sources/sources/ab/abcd/test.txt",
        ), patch(
            "app.services.rag_service.insertDocumentChunks"
        ):
            svc = RagService()
            docSvc = self._mock_doc_svc_with_create()
            docSvc.listDocuments = AsyncMock(return_value=[existing])
            svc._doc_svc = docSvc
            await svc.ingestDocument(
                mock_session,
                content=b"dummy",
                filename="test.txt",
                mime_type="text/plain",
                actor=CurrentUser(userId="test-user"),
            )

        # Assert：走的是 updateDocument，且写的是真实 URL
        docSvc.updateDocument.assert_awaited()
        updateDto = docSvc.updateDocument.await_args.args[2]
        assert updateDto.storage_url == "s3://qa-knowledge-sources/sources/ab/abcd/test.txt"
        assert updateDto.content_hash is not None
        # actor 必须传（原实现漏传 → TypeError → 被 except 吞掉，分支从未生效）
        assert docSvc.updateDocument.await_args.kwargs["actor"] is not None

    @pytest.mark.asyncio
    async def test_ingest_empty_document_raises(self) -> None:
        """解析出空块列表必须显式报错，不能往下走进 Milvus。"""
        # Arrange
        mock_session = MagicMock()

        # Act / Assert
        with patch(
            "app.services.rag_service.parse_document",
            new_callable=AsyncMock,
            return_value=[],
        ), patch("app.services.rag_service.insertDocumentChunks") as mock_insert:
            svc = RagService()
            svc._doc_svc = self._mock_doc_svc_with_create()
            with pytest.raises(RagError, match="文档内容为空"):
                await svc.ingestDocument(
                    mock_session,
                    content=b"",
                    filename="empty.txt",
                    mime_type="text/plain",
                    actor=CurrentUser(userId="test-user"),
                )
        mock_insert.assert_not_called()
```

> `updateDocument(session, id, dto)` 的位置参数顺序若与 `args[2]` 不符，以 `rag_service.py` 里的实际调用为准调整。
> 同文件其他用例（Milvus 失败、embedding 失败等）里的 `parse_document` mock 与 `split_by_paragraphs` 返回值**全部**要按上面的形状改（`list[TextBlock]` + 带 metadata 的 chunk），否则签名变更会让它们一起挂掉。

再新建端到端集成测试 `backend/app/tests/integration/test_rag_ingest_provenance.py`（真实 PostgreSQL + 完整 API 链路）。它**不**重复验 Milvus 侧的落库与回读 —— 那是 Task 3 `test_milvus_document_fields` 的职责；这里验的是 `rag_service` 这一层的接线：有没有把 `chunk.metadata` 接进 Milvus 记录、把真实 url/hash 接进 `document_catalog`。

先把 `test_rag_api.py` 里已有的 `mockEmbeddingService` **上移**到 `backend/app/tests/integration/conftest.py`（两个文件共用，不复制第二份），并删掉 `test_rag_api.py` 中的原定义与随之无用的 `AsyncMock` import（若该文件仍在别处使用则保留）：

```python
@pytest.fixture
def mockEmbeddingService():
    """EmbeddingService 的替身，模拟 generateEmbedding。

    向量是固定常数（不是真实 LLM 输出），但保证相同文本得到相同向量，
    这样 searchDocuments 能用相同 query 命中刚刚 ingest 的文档。
    """
    svc = AsyncMock()

    async def fake_embed(text: str) -> list[float]:
        # 用文本长度作种子，让同一文本生成同一向量（保证 search 能命中）
        seed = sum(ord(c) for c in text) % 100
        return [float(seed) / 100.0 + 0.001 * i for i in range(1024)]

    svc.generateEmbedding = fake_embed
    return svc
```

（`conftest.py` 顶部需能拿到 `AsyncMock`；没有就补 `from unittest.mock import AsyncMock`。）

新建 `backend/app/tests/integration/test_rag_ingest_provenance.py`：

```python
"""RAG 上传链路的溯源端到端（P0 溯源地基）。

真实 PostgreSQL + 完整 API 链路：POST /documents/upload → rag_service →
document_catalog。MinIO 用假客户端（patch 点与 test_wiki_import_catalog
一致），Milvus 的 insert 用替身捕获 —— 定位符在 Milvus 侧的落库与回读由
test_milvus_document_fields 负责，本文件只验 rag_service 这层的接线。
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text


@pytest.fixture
def fakeMinio(monkeypatch):
    """假 MinIO：不联网络，只记录调用。

    用 MagicMock 而不是替身函数：本用例只关心「写没写、写的是不是原始
    字节」，不该顺带把 putSourceObject 对 SDK 的调用形状（位置参还是
    关键字参）钉成测试契约 —— 那是 Task 4 单测的职责。
    """
    fake = MagicMock()
    fake.bucket_exists.return_value = True
    monkeypatch.setattr(
        "app.infrastructure.object_storage._getClient", lambda: fake
    )
    return fake


def _writtenBytes(call) -> bytes | None:
    """从 ``put_object`` 的调用里取出被写入的字节。

    不假设对象流是第几个位置参数：``putSourceObject`` 对 SDK 的调用形状是
    Task 4 单测的契约，这里只关心「写进去的是不是原始字节」。

    直接写 ``content in call.args`` 是**不成立**的 —— 实现传的是
    ``io.BytesIO(content)``，与裸 bytes 做身份/相等比较永远为 False，
    断言会无条件失败（已实测）。
    """
    for value in (*call.args, *call.kwargs.values()):
        if hasattr(value, "getvalue"):
            return value.getvalue()
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
    return None


class TestRagUploadProvenance:
    @pytest.mark.asyncio
    async def test_upload_persists_source_bytes_and_locators(
        self, client, dbSession, fakeMinio, mockEmbeddingService
    ) -> None:
        # Arrange
        # 夹具用 Markdown 而不是 .txt：TXT/MD 都没有页码，.txt 更连章节都没有，
        # 那样「定位符接进去了」这条就只能退化成断言 None，等于没验。
        # 带 `#` 章节的 MD 能同时验到 section_name 的精确透传与段号。
        content = (
            "# 第一章 供应商准入\n\n"
            "第一段：供应商准入需注册资本不少于一千万。\n\n"
            "第二段：质量协议每年复核一次。"
        ).encode("utf-8")
        files = {"file": ("provenance.md", content, "text/markdown")}

        # Act
        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ), patch(
            "app.services.rag_service.insertDocumentChunks"
        ) as mockInsert:
            resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L1"},
                files=files,
            )

        # Assert 1：源文件真的写进了对象存储，且写的是原始字节
        assert resp.status_code == 201, resp.text
        assert fakeMinio.put_object.call_count == 1
        putArgs = fakeMinio.put_object.call_args
        written = _writtenBytes(putArgs)
        assert written is not None, "put_object 没收到文件内容流"
        assert written == content, "写入对象存储的不是原始字节"

        body = resp.json()
        assert body["storage_url"].startswith("s3://")

        # Assert 2：catalog 落的是真实 url + 64 位摘要（不是 milvus://N_chunks）
        assert re.fullmatch(r"[0-9a-f]{64}", body["content_hash"])
        rows = await dbSession.execute(
            text(
                "SELECT storage_url, content_hash FROM document_catalog "
                "WHERE document_id = :docId"
            ),
            {"docId": body["document_id"]},
        )
        row = rows.one()
        assert row.storage_url == body["storage_url"]
        assert row.content_hash == body["content_hash"]
        assert not row.storage_url.startswith("milvus://")

        # Assert 3：定位符接进了 Milvus 记录
        # 按 Markdown 的真实语义断言：有章节、有段号、**没有页码**（只有 PDF 有页）。
        # 哨兵（-1 / ""）是 insertDocumentChunks 的职责，而它在本用例里被替身接管，
        # 所以这里拿到的是 chunk.metadata 的原样值 —— 那正是本层要验的接线。
        record = mockInsert.call_args.args[0][0]
        assert record["section_name"] == "第一章 供应商准入"
        assert record["paragraph_no"] == 1
        assert record["page_number"] is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_rag_service.py -q
```

Expected: FAIL — `TypeError: object of type 'list' has no len()` / `AttributeError: 'list' object has no attribute 'strip'`

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_rag_ingest_provenance.py -q
```

Expected: FAIL — `assert 0 == 1`（`putSourceObject` 尚未被调用，源文件没留存）

- [ ] **Step 3: 实现**

修改 `backend/app/services/rag_service.py`。顶部 import 补：

```python
from app.infrastructure.object_storage import (
    ObjectStorageError,
    buildSourceObjectName,
    hashContent,
    putSourceObject,
)
```

把 `# 1. 解析` 到 `# 2. 分块` 一段（约 `:94-106`）替换为：

```python
        # 1. 解析（返回带定位符的文本块）
        try:
            blocks = await parse_document(content, mime_type, filename)
        except DocumentParserError as e:
            raise RagError(f"文档解析失败: {e}") from e

        if not blocks:
            raise RagError("文档内容为空，无法入库")

        # 2. 源文件留存（内容寻址；失败必须显式，不得静默跳过）
        contentHash = hashContent(content)
        objectName = buildSourceObjectName(contentHash, filename)
        try:
            storageUrl = putSourceObject(objectName, content, mime_type)
        except ObjectStorageError as e:
            raise RagError(f"源文件存储失败: {e}") from e

        # 3. 分块
        chunks: list[Chunk] = split_by_paragraphs(blocks)
        if not chunks:
            raise RagError("分块结果为空")
```

（后续原步骤号顺延，`# 3. 生成 embedding` → `# 4.`，`# 4. 写入 Milvus` → `# 5.`，`# 5. 写入 document_catalog` → `# 6.`。）

Milvus 记录构造补齐定位符：

```python
        records: list[dict] = []
        for chunk, emb in zip(chunks, embeddings):
            records.append({
                "document_id": doc_id,
                "chunk_id": chunk.chunk_id,
                "chunk_text": chunk.text,
                "chunk_sequence": chunk.sequence,
                "effective_date": effective_str,
                "security_level": security_level,
                "page_number": chunk.metadata.get("page_number"),
                "section_name": chunk.metadata.get("section_name"),
                "paragraph_no": chunk.metadata.get("paragraph_no"),
                "embedding": emb,
            })
```

`DocumentCreate` 与 `DocumentUpdate` 两处改为真实值：

```python
                    storage_url=storageUrl,
                    content_hash=contentHash,
```

```python
        else:
            # 已存在：刷新真实存储信息。
            # 原实现写的是 f"milvus://{len(chunks)}_chunks" 这种假 URL，而且
            # **漏传了 actor** —— updateDocument(session, id, dto, actor) 四个
            # 参数，只传三个必然 TypeError，又被下面的 except Exception: pass
            # 吞掉，所以这个分支在生产里从未真正生效过。两处一起修。
            await self._doc_svc.updateDocument(
                session,
                existing_doc.id,
                DocumentUpdate(storage_url=storageUrl, content_hash=contentHash),
                actor=actor,
            )
```

（**删除** 原来的 `try/except Exception: pass`。让异常冒泡为 500 并在日志中留下上下文，胜过一次静默的元数据丢失。已核对该分支的调用签名：`document_service.py:145` 的 `updateDocument(self, session, id, dto, actor)`。）

同时把返回值补上两个键，供测试与调用方使用：

```python
        return {
            "document_id": doc_id,
            "chunks": len(chunks),
            "status": "ingested",
            "storage_url": storageUrl,
            "content_hash": contentHash,
        }
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_rag_service.py app/tests/integration/test_rag_ingest_provenance.py app/tests/integration/test_rag_api.py -q
```

Expected: PASS（三条都要跑 —— 前两条是本任务的产出，`test_rag_api.py` 是确认 fixture 上移没打断既有用例）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/rag_service.py backend/app/tests/unit/test_rag_service.py backend/app/tests/integration/test_rag_ingest_provenance.py backend/app/tests/integration/conftest.py backend/app/tests/integration/test_rag_api.py
git commit -m "fix(wiki): rag_service 写真实 storage_url/content_hash 并消除静默失败"
```

---

## Task 6: Wiki 导入路径留存源文件 + 登记 `document_catalog`

**Files:**
- Modify: `backend/app/api/v1/wiki_import.py` — `preview-file` 落库（含两处 docstring 契约改写）
- Modify: `backend/app/services/wiki_import_service.py` — `parseFile` 适配 `TextBlock` + 新增 `persistSourceFile`
- Modify: `backend/app/services/messages_zh.py` — 新增 `MSG_WIKI_IMPORT_SOURCE_STORE_FAILED`
- Create: `backend/app/services/wiki_catalog_registrar.py`
- Test: `backend/app/tests/integration/test_wiki_import_catalog.py`

**Interfaces:**
- Consumes: `parse_document` 新返回类型（Task 1）、`object_storage`（Task 4）
- Produces: `WikiImportService.persistSourceFile(session, *, content, mime_type, filename, actor) -> str`（返回 `storage_url`）

> ### ⚠️ 设计决定（2026-09-12 用户选定，本任务据此重写）
>
> Spec §4.1 的 C5 原文写「Wiki 导入路径也注册 `document_catalog`」，暗示在 `execute` 挂上即可。**读码后确认这不可直接实现**：`execute()` 的输入是 `dto.drafts`（客户端回传的**纯文本草稿**），**从不接触文件字节** —— 拿不到 `content`，就算不出文件哈希、存不了源文件。
>
> 字节只在 `preview-file` 手里。三个候选方案已提交用户选择，**用户选定「preview-file 就落库」**：
>
> - 在 `preview-file` 存对象 + 登记 `document_catalog`，`content_hash` 与 `storage_url` **全由服务端计算**。
> - `execute` **不携带任何溯源字段** —— 零客户端可信数据，`execute` 的职责与签名不变。
> - 理由：本仓库已有同一原则的先例 —— `WikiImportFileParseRead` 的 docstring 明确写 `source_type` 由后端判定而非前端自报：「让调用方自报等于允许『上传 .pdf 却记成 MARKDOWN』，台账就不可信了」。`content_hash` / `storage_url` 是同一性质的溯源字段，交由客户端回传会犯同一个错。
>
> **契约变更（必须诚实记录，两处 docstring 都要改）**：模块 docstring 第 3 条与 `previewImportFile` 的 docstring 原写「**不落库**、不调模型」。本任务后它**会落库**（对象存储 + 一行 `document_catalog`），仍**不调模型**。契约与实现背离比改动本身更危险，必须同步。
>
> **代价（已知并接受）**：用户**预览了但没导入**的文件，也会留下一个对象与一行 catalog。内容寻址使重复预览不产生重复对象，catalog 登记再按 `content_hash` 幂等一层。若日后要「预览不留痕」，那是另一个变更。

> **顺序至关重要**：必须**先 `parseFile` 成功、再存对象**。反过来的话，每个损坏/加密/格式不符的文件都会在对象存储里留下一份永远无人引用的垃圾。`MAX_UPLOAD_BYTES` 检查同样排在存之前（它已在最前面）。
>
> **MinIO 故障必须显式失败**（spec §4.5）：写对象失败即 503，**不得**降级为「预览成功但其实没存」。代价是预览可用性从此与 MinIO 绑定 —— 这是「预览即持久化」方案的固有代价，接受它。

> **「查看」怎么走**：落库后源文件即出现在既有文档库（`GET /documents` 读 `document_catalog`），**不需要新建查看页面、不需要新建端点**。P0 不新增任何前端。

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/integration/test_wiki_import_catalog.py`。`client` / `dbSession` fixture 取自同目录 `conftest.py`（与 `test_wiki_import_api.py` 同一个）：

```python
"""Wiki 导入路径留存源文件 + 登记 document_catalog（P0 溯源地基）。

真实 PostgreSQL + 完整 API 链路。**MinIO 客户端被替换为假实现** —— 不联外网，
但保留完整调用链（内容哈希 → 内容寻址对象名 → catalog 写入），与既有集成测试
替换假 LLM 客户端（``_INVOKER_CLIENT``）是同一思路：外部服务走假实现，
数据库与 API 链路保持真实。真实 MinIO 的端到端验证由 Task 7 的真实数据脚本承担。
"""

from __future__ import annotations

import hashlib
from typing import Iterator
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DocumentType
from app.domain.models import DocumentCatalog

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/import"
_CLIENT = "app.infrastructure.object_storage._getClient"
_FILE_URL = f"{_BASE}/preview-file"


class _FakeMinio:
    """记录 put_object 的假客户端（形状对齐 minio.Minio 的被调用面）。"""

    def __init__(self) -> None:
        self.putCalls: list[dict] = []

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def make_bucket(self, bucket: str) -> None:
        raise AssertionError(f"桶 {bucket} 应已存在，不该被创建")

    def put_object(self, bucket, objectName, data, length=None, content_type=None):
        self.putCalls.append(
            {
                "bucket": bucket,
                "objectName": objectName,
                "content": data.read(),
                "contentType": content_type,
            }
        )


@pytest.fixture(autouse=True)
def fakeMinio() -> Iterator[_FakeMinio]:
    """模块级 autouse：本模块每个用例都不联真实 MinIO。"""
    fake = _FakeMinio()
    with patch(_CLIENT, return_value=fake):
        yield fake


async def _upload(
    client: AsyncClient, name: str, body: bytes, mime: str = "text/plain"
):
    return await client.post(_FILE_URL, files={"file": (name, body, mime)})


async def test_preview_file_stores_source_and_registers_catalog(
    client: AsyncClient, dbSession: AsyncSession, fakeMinio: _FakeMinio
) -> None:
    """上传后对象已写入，catalog 有一行指向它，且 hash 是**文件字节**的摘要。"""
    # Arrange
    body = "## 准入规则\n\n注册资本 >= 1000 万。".encode("utf-8")
    expectedHash = hashlib.sha256(body).hexdigest()
    expectedObject = f"sources/{expectedHash[:2]}/{expectedHash}/规则.txt"

    # Act
    resp = await _upload(client, "规则.txt", body)

    # Assert —— 对象侧
    assert resp.status_code == 200
    assert len(fakeMinio.putCalls) == 1
    assert fakeMinio.putCalls[0]["objectName"] == expectedObject
    assert fakeMinio.putCalls[0]["content"] == body
    assert fakeMinio.putCalls[0]["bucket"] == "qa-knowledge-sources"

    # Assert —— 目录侧
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.document_name == "规则.txt"
    # 关键：是**文件字节**的摘要，不是草稿文本的摘要（后者是旧方案的妥协）
    assert row.content_hash == expectedHash
    assert row.storage_url == f"s3://qa-knowledge-sources/{expectedObject}"
    # 不能是 source_type（"MARKDOWN"）—— 那既不是合法枚举值也语义不符
    assert row.document_type == DocumentType.OTHER


async def test_response_contract_unchanged(client: AsyncClient) -> None:
    """响应形状不变：仍是 text + sourceType，**没有新增字段**。

    落库是纯服务端副作用，客户端不需要回传任何东西 —— 这正是选它的理由。
    若将来给响应加了字段，本用例会失败，逼人回来看这里。
    """
    # Act
    resp = await _upload(client, "规则.txt", "## 准入规则\n\n注册资本 >= 1000 万。".encode())

    # Assert
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"text", "sourceType"}
    assert "注册资本 >= 1000 万。" in body["text"]
    assert body["sourceType"] == "MARKDOWN"


async def test_same_file_twice_does_not_duplicate_catalog(
    client: AsyncClient, dbSession: AsyncSession, fakeMinio: _FakeMinio
) -> None:
    """同一份文件重复上传，catalog 不应新增第二行。"""
    # Arrange
    body = "# 同一份\n\n同样的内容".encode()

    # Act
    first = await _upload(client, "same.md", body)
    second = await _upload(client, "same.md", body)

    # Assert
    assert first.status_code == 200
    assert second.status_code == 200
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert len(rows) == 1, f"同 hash 重复上传产生了 {len(rows)} 行 catalog"


async def test_different_files_produce_separate_rows(
    client: AsyncClient, dbSession: AsyncSession, fakeMinio: _FakeMinio
) -> None:
    """不同内容应是两行 —— 否则上面的去重可能只是「永远只写一行」的假象。"""
    # Act
    await _upload(client, "a.md", "# a\n\nA 的内容".encode())
    await _upload(client, "b.md", "# b\n\nB 的内容".encode())

    # Assert
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert len(rows) == 2
    assert {r.document_name for r in rows} == {"a.md", "b.md"}


async def test_rejected_upload_stores_nothing(
    client: AsyncClient, dbSession: AsyncSession, fakeMinio: _FakeMinio
) -> None:
    """被拒绝的文件不留痕：格式不支持 → 422，且对象存储与 catalog 都是空的。

    这条钉死的是**顺序**：留存必须发生在解析成功之后。反过来的实现
    （先存再解析）会让每个格式不符/损坏的文件都在对象存储里留下垃圾，
    而其余用例全都照样通过 —— 所以必须有这一条。
    """
    # Act
    resp = await _upload(client, "note.xyz", b"anything", "application/octet-stream")

    # Assert
    assert resp.status_code == 422
    assert fakeMinio.putCalls == []
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert rows == []
```

> 五个用例覆盖：留存发生且 hash 取自文件字节、响应契约未被顺带改宽、同内容幂等、不同内容确实两行、被拒文件不留痕。第四、五条是必须的 —— 少了它们，一个「永远只写一行」的实现、或一个「先存后解析」的实现，都能在其他用例上全绿。
>
> 端点路径是 `/preview-file`（连字符，非 `/preview/file`），响应键是 `text` / `sourceType`。以上均取自 `test_wiki_import_api.py` 实际用例，非推测。
>
> 本任务**不需要假 LLM**：`preview-file` 从头到尾不调模型（`execute` 才调）。所以这里比 `test_wiki_import_api.py` 简单。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/integration/test_wiki_import_catalog.py -q
```

Expected: FAIL —— `putCalls` 为空、catalog 行数为 0（实现尚未落库）

- [ ] **Step 3: 适配 `parseFile` 到 `TextBlock`**

`wiki_import_service.py:205` 改为：

```python
        blocks = await parse_document(content, mime_type, filename)
        # 预览仍要扁平文本（前端按段落编辑），由带定位的块拼回。
        text = "\n\n".join(b.text for b in blocks)
        return text, sourceTypeFromFilename(filename, mime_type)
```

- [ ] **Step 4: 新增 `persistSourceFile`**

在 `WikiImportService` 里新增（放在 `parseFile` 之后）：

```python
    async def persistSourceFile(
        self,
        session: AsyncSession,
        *,
        content: bytes,
        mime_type: str,
        filename: str,
        actor: int | None,
    ) -> str:
        """留存源文件并登记 document_catalog，返回 ``storage_url``。

        内容寻址：同一份文件重复上传落到同一对象名，天然去重；catalog 登记
        再按 ``content_hash`` 幂等一层。两层都不依赖调用方传任何东西进来 ——
        哈希与 URL 全部由服务端从**真实字节**算出。

        Raises:
            ObjectStorageError: MinIO 不可用或写入失败（API 层转 503）
        """
        contentHash = hashContent(content)
        objectName = buildSourceObjectName(contentHash, filename)
        storageUrl = putSourceObject(objectName, content, mime_type)
        await self._catalog.upsertByContentHash(
            session,
            document_name=filename,
            content_hash=contentHash,
            storage_url=storageUrl,
            actor=actor,
        )
        await session.commit()
        return storageUrl
```

并在 `WikiImportService.__init__` 注入 catalog 依赖（沿用 `classifier` 的既有注入风格）：

```python
    def __init__(
        self,
        *,
        classifier: AutoClassifier | None = None,
        catalog: WikiCatalogRegistrar | None = None,
    ) -> None:
        self._classifier = classifier or AutoClassifier()
        self._catalog = catalog or WikiCatalogRegistrar()
        self._pageService = WikiPageService()
```

模块顶部补 import：

```python
from app.infrastructure.object_storage import (
    buildSourceObjectName,
    hashContent,
    putSourceObject,
)
from app.services.wiki_catalog_registrar import WikiCatalogRegistrar
```

- [ ] **Step 5: API 层落库 + 契约 docstring 改写**

`wiki_import.py` 的 `previewImportFile` 增加两个依赖（`import` 段补 `ObjectStorageError` 与 `MSG_WIKI_IMPORT_SOURCE_STORE_FAILED`）：

```python
@router.post("/preview-file", response_model=WikiImportFileParseRead)
@limiter.limit(rateLimitValue)
async def previewImportFile(
    request: Request,
    file: UploadFile,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> WikiImportFileParseRead:
```

成功分支改为（**在 `parseFile` 之后**）：

```python
    # 解析成功后才留存：损坏/格式不符的文件不该在对象存储里留下垃圾。
    # MinIO 故障显式失败（503），不降级为「预览成功但其实没存」。
    try:
        await _importService.persistSourceFile(
            db,
            content=content,
            mime_type=mime,
            filename=fname,
            actor=user.dbUserId,
        )
    except ObjectStorageError:
        logger.exception("知识导入源文件留存失败: filename=%s mime=%s", fname, mime)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_WIKI_IMPORT_SOURCE_STORE_FAILED,
        ) from None

    return WikiImportFileParseRead(text=text, source_type=sourceType)
```

**两处 docstring 必须同改**（契约背离比改动本身危险）：

1. `previewImportFile` 首行：`"""上传文件 → 纯文本 + 来源类型（不落库、不调模型）。` 改为
   `"""上传文件 → 纯文本 + 来源类型（落对象存储与文档目录，不调模型）。`
2. 模块 docstring 第 3 条：`3. ``POST /wiki/import/preview-file`` 上传文件 → 纯文本（不落库、不调模型）`
   改为 `3. ``POST /wiki/import/preview-file`` 上传文件 → 纯文本（**留存源文件**、不调模型）`。

并在模块 docstring 补一段说明留存语义：

```
上传路径会把源文件按内容寻址存入对象存储，并登记一行 ``document_catalog``
（原文件因解析成功才会走到这一步）。因此「预览」在上传路径上是**有副作用**的：
预览过但未导入的文件同样会留存。这是 2026-09-12 的刻意决定 —— 溯源价值高于
「预览不留痕」，且内容寻址让重复预览不产生重复对象。
```

`messages_zh.py` 新增（与既有 `MSG_WIKI_IMPORT_*` 同段落）：

```python
MSG_WIKI_IMPORT_SOURCE_STORE_FAILED = (
    "源文件留存失败，请稍后重试；若持续失败请联系管理员。"
)
```

- [ ] **Step 6: 新建 `wiki_catalog_registrar.py`**

单独成文件而非塞进 `wiki_import_service`：后者已 520 行，接近 800 行上限；登记逻辑与导入编排无共同状态，拆开后各自可测。

```python
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
```

> **列约束已核对**（`backend/app/domain/models.py:1265-1288`）：`version` / `status` / `security_level` 有列默认值，可不传；`document_id` 唯一非空、`document_name` 非空、`document_type` 非空且为 `DocumentType` 枚举 —— 上面三处均已显式赋值，无需再补。

- [ ] **Step 7: 运行测试确认通过**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/integration/test_wiki_import_catalog.py -q
```

Expected: PASS（5 个用例）

- [ ] **Step 8: 回归既有 wiki 导入测试**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/ -q -k "wiki_import"
```

Expected: PASS

> **注意**：既有 `test_wiki_import_api.py` 里凡是打 `/preview-file` 的用例，从本任务起会**真的尝试连 MinIO**。若它们未 patch 客户端，会由「本来就不该联外网」变成失败 —— 这属于预期内的连带影响，不是回归。**修法是在那些用例里一并 patch** `app.infrastructure.object_storage._getClient`（与 `test_wiki_import_catalog.py` 的 autouse fixture 同款），**不要**改成跳过断言。

- [ ] **Step 9: 提交**

```bash
git add backend/app/api/v1/wiki_import.py backend/app/services/wiki_import_service.py \
        backend/app/services/wiki_catalog_registrar.py backend/app/services/messages_zh.py \
        backend/app/tests/integration/test_wiki_import_catalog.py
git commit -m "feat(wiki): 上传路径留存源文件 + 登记 document_catalog（preview-file 落库）"
```

---

## Task 7: 真实数据验证、变更记录与部署

**Files:**
- Create: `backend/scripts/wiki_provenance_realdata.py`
- Create: `Harness/changes/feat-wiki-provenance/summary.md`
- Modify: `Harness/changes/` 索引（若有）

**Interfaces:**
- Consumes: 全部前序任务
- Produces: Harness 开发门禁所需的真实数据验证脚本与九段变更记录

> **部署方式变更（重要）**：本计划新增了 `minio` 依赖。`scripts/deploy_backend.sh` 只 `docker cp` `app/`+`scripts/`+`alembic/`，**不安装依赖**，因此**不能**用它部署本变更。必须走完整镜像重建。
>
> 已知风险：Dockerfile 的 base image 拉取曾因 Docker Hub 不可达而失败（Dockerfile 里保留了多处 aliyun 镜像源与重试逻辑，就是这个原因）。若 `docker compose build backend` 失败，先确认 base image 是否已在本地缓存（`docker images | grep python`）。

- [ ] **Step 1: 写真实数据验证脚本**

新建 `backend/scripts/wiki_provenance_realdata.py`：

```python
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

    # --- 4. MinIO：读回的字节必须与上传一致 ---
    s3Prefix = f"s3://{DEFAULT_BUCKET}/"
    if catalog is not None and (catalog.storage_url or "").startswith(s3Prefix):
        objectName = catalog.storage_url[len(s3Prefix):]
        readBack = getSourceObject(objectName)
        if readBack != content:
            failures.append("MinIO 读回内容与上传字节不一致")
        else:
            print(f"minio.read_back     = {len(readBack)} bytes（与上传一致）")

    # --- 5. 自清：门禁不该给正式库留痕 ---
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
    reportlab 是已声明依赖（`reportlab>=4.2.0`，实机 5.0.0，已验证可导入）。

    ⚠️ CJK 字体必须显式指定：默认 Helvetica 编不了中文，reportlab 会静默
    替换成豆腐块，抽出来是 `■■■■■`。`STSong-Light` 是自带的 CID 字体。
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
```

- [ ] **Step 2: 重建并部署**

**必须在跑门禁之前做。** 脚本依赖新的 `object_storage` 模块与 `minio` 包，
而这两样只有重建镜像后才进得了容器；顺序反了，Step 3 只会得到
`ModuleNotFoundError`，看上去像脚本写错了。

```bash
cd /path/to/repo
# 回滚快照必须在 build **之前** 打：build 之后 `:latest` 已是 P0 新镜像，
# 再打 tag 只是给新镜像换个名字，回滚时拿不到旧的那份。
docker tag qa-system-backend:latest qa-system-backend:pre-p0
docker compose -f docker/docker-compose.yml build backend
docker compose -f docker/docker-compose.yml up -d qa-objects backend
docker exec qa-backend python -c "import minio; print('minio ok')"
docker exec qa-backend alembic current    # 本计划无新迁移，应停在既有 head
docker exec qa-backend python scripts/rebuild_document_collection.py
```

Expected: `qa-objects` 起来且健康；`qa-backend` 健康；`minio ok`；集合重建成功

- [ ] **Step 3: 跑验证脚本**

```bash
docker cp backend/scripts/wiki_provenance_realdata.py qa-backend:/app/scripts/
docker exec qa-backend python scripts/wiki_provenance_realdata.py
```

Expected: 打印 `ingest.*` / `milvus.chunk_count` / `milvus.page_numbers` /
`catalog.storage_url` / `catalog.content_hash` / `minio.read_back`，
末尾 `✅ P0 溯源地基真实数据验证通过（已清理本次写入）`

- [ ] **Step 4: 验证回滚路径**

```bash
docker image inspect qa-system-backend:pre-p0 --format '{{.Id}}'   # 应为 build 之前的那份
# 回滚：docker tag qa-system-backend:pre-p0 qa-system-backend:latest && docker compose ... up -d backend
#       （或 git revert 本计划的提交序列后重建镜像）
```

> P0 无 PG 迁移，回滚不涉及数据。**唯一不可逆项**是 Milvus 集合重建：重建会丢弃
> 旧 collection 的所有向量，**且重建前该集合并非空**——重建前已实测有 **333** 条
> entity，并于 2026-09-12 19:53 导出快照到
> `backups/milvus/document_embeddings_20260912_1953.json.gz`（`num_entities: 333`）。
> 快照只存了标量字段与向量本身，**恢复需另写导入脚本**，故「有快照」不等于「可一键回滚」。
> 这一条必须如实写进变更记录，不得写成「无数据损失」。
> **MinIO 里的源文件不受回滚影响**（保留是好事）。

- [ ] **Step 5: 写九段变更记录**

新建 `Harness/changes/feat-wiki-provenance/summary.md`，按 `Harness/changes/_template/summary.md` 的九段结构填写。**第 9 段「真实数据验证报告」必须包含 Step 2 的完整实际输出**——照抄终端输出，不得概括。同时必须记录：

- **备份门禁缺口**：PG 备份 cron 已确认静默失效（launchd 契约断裂，根因是 `/etc/crontab` 缺失），用户 2026-09-12 决定维持手动。`scripts/backup_objects.sh` 同样以手动形态交付。此缺口不得粉饰。
- **spec §4.1 C5 的修正**（见 Task 6 的说明）。
- **部署方式变更**：本变更不能走 `deploy_backend.sh`。
- **Milvus 集合重建的不可逆性**：重建前集合含 333 条 entity（非空），快照在
  `backups/milvus/document_embeddings_20260912_1953.json.gz`，恢复需另写导入脚本。
  不得写成「无数据损失」。

- [ ] **Step 6: 提交**

```bash
git add backend/scripts/wiki_provenance_realdata.py Harness/changes/feat-wiki-provenance/
git commit -m "chore(wiki): P0 真实数据验证脚本 + 变更记录"
```

---

## 验收对照（spec §十）

| 验收信号 | 由哪个任务交付 |
|---|---|
| Milvus chunk 带正确 `page_number` | Task 3 Step 7 |
| `document_catalog` 有真实 `storage_url` + 非空 `content_hash` | Task 5（RAG 路径）、Task 6（wiki 路径） |
| MinIO 桶内可回读，且内容与上传字节一致 | Task 4 Step 6、Task 7 Step 2 |
| wiki 上传路径同样留存源文件，`content_hash` 取自**文件字节**（非草稿文本） | Task 6 Step 1 / Step 7 |
| 同一文件重复上传不产生第二行 catalog | Task 6 Step 1 / Step 7 |
| 被拒文件（格式不符）在对象存储与 catalog 都不留痕 | Task 6 Step 1 / Step 7 |

## 不做的事（范围边界）

- **不产生任何 PG 迁移**（spec D2-2）。`content_hash` 唯一约束推到 P1。
- 不实现 preview→execute 的上传令牌改造（Task 6 已说明理由）。
- 不改前端。
- 不做 `claim`/`evidence` 写入（那是 P3）。

---

## 跨计划协调（与 P1 / P3 的接口）

P0 / P1 / P3 是三份独立计划，**执行顺序未定**。以下是它们之间的接触点，执行任一计划前必须核对。

### 1. `document_catalog.content_hash` 唯一索引 —— P1 建，P0 不建

P1（`2026-09-12-wiki-dedup-p1.md`）的迁移 `0061_wiki_dedup` 会给
`document_catalog.content_hash` 建**唯一索引** `uq_document_catalog_content_hash`
（spec §4.7 把该约束明确推迟到 P1「只付一次迁移成本」）。

**P0 的 Task 6 不创建该索引，也不要"顺手"补上。** P1 已实测：当前该列存在、表 0 行、
尚无唯一索引，所以那个索引由 P1 建成本为零。两处都建会让定义各自漂移 ——
`IF NOT EXISTS` 会让重复创建静默通过，看起来没事。

P0 的 `WikiCatalogRegistrar.upsertByContentHash` 用的是 SELECT-then-INSERT，
**不依赖**唯一约束来保证幂等；P1 的索引落地后它只是多了一层并发兜底。

### 2. 摘要口径必须一致 —— 两份实现，同一算法

P0 与 P1 各有一个摘要函数：

| 计划 | 函数 | 定义 |
|---|---|---|
| P0（Task 4） | `object_storage.hashContent(content: bytes) -> str` | `sha256(content).hexdigest()` |
| P1（Task 1） | `wiki_page_service.contentHashOf(content: str) -> str` | `sha256(content.encode("utf-8")).hexdigest()` |

两者逐字节等价：`contentHashOf(s) == hashContent(s.encode("utf-8"))`。

**归口：以 P0 的 `hashContent(content: bytes)` 为唯一实现。** P1 的 `contentHashOf`
落地时退化为薄包装 `return hashContent(content.encode("utf-8"))`，不要再留第二份
`sha256` 调用 —— 两份实现即使当下等价，也会各自漂移，而漂移了不会报错，只会让
两侧的比对永远不相等。

**P0 的 Task 6 摘要的是上传文件的原始字节**（`hashContent(content)`，`content: bytes`），
与客户端回传的草稿文本无关 —— Task 6 的落点在 `preview-file`（见 `## 不做的事` 起
的说明），那里能拿到 `UploadFile` 的字节。**PDF/DOCX 的 `hashContent(文件字节)` 与
`contentHashOf(解析后的文本)` 不是同一个值，这是对的**：源文件的内容寻址本就该按
字节算。所以 Task 6 不依赖 P1 的函数，两者互不阻塞。

Task 6 的用例已断言「hash 取自文件字节」——保留它。只断言长度挡不住口径漂移。

### 3. `wiki_import_service.execute` —— P0 已**不再**是共同改动点

本节曾提示 P0 与 P1 都要改 `execute`（P0 在任务台账落库之后追加 catalog 登记）。
**Task 6 改为在 `preview-file` 落库之后，这个接触点消失了**：P0 完全不碰
`wiki_import_service.execute`，P1 Task 4 可以独立重写它的循环体与 `_importOne`。
两份计划依然可任意顺序落地。

仍存在的一处**弱**耦合：P0 的 `WikiCatalogRegistrar` 与 P1 都会写 `document_catalog`，
但写的是不同行 —— P0 按**源文件字节**登记（`preview-file` 路径），P1 按 **wiki 页文本**
登记。P0 用 SELECT-then-INSERT，不依赖 P1 的唯一索引来保证幂等。

若将来有人把 P0 的登记点移回 `execute`，需重新评估本节。

另一个交互（仍然成立）：P1 的导入测试用 `autoClassify: false`（不调 LLM）；P0 的
Task 6 测试传 `modelId` 并 patch `_INVOKER_CLIENT`。两者各自独立成立，互不影响。

### 4. `TextBlock` 只属于 P0

P1 与 P3 都**不依赖** `TextBlock` / `Chunk.metadata` / MinIO 管线（P1 计划已显式声明解耦）。
P3 的 `evidence` 写入若要填 `page_number` / `section_name` / `paragraph_no`，来源正是 P0 落进
Milvus 的这三列 —— 那是**数据上的**依赖，不是代码依赖，故 P3 不阻塞于 P0，但 P0 未落地时
P3 写出的 evidence 定位符只能是空值。这一点在 P3 计划里核对。
