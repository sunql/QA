# P0 溯源地基 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每段入库文本携带页码/章节/段号定位符，并让 `document_catalog` 记下源文件的**确定性存储位置占位**与内容摘要，使后续证据链有出处可填。

**Architecture:** 在摄入链路上补三处丢失点：(1) `parse_document` 从返回扁平 `str` 改为返回带定位的 `list[TextBlock]`；(2) `Chunk` 用既有的未使用 `metadata` 字段承载定位符，落进 Milvus 新字段；(3) 新增 `source_locator` **纯函数**模块算好源文件的预留存储键与摘要，`document_catalog` 写 `storage_url`（占位）+ `content_hash`。**源文件字节暂时不存储**（用户 2026-09-12 决定），只落解析后的内容。全程无 PG 迁移、无新增依赖、无新容器。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 async / pypdf / python-docx / pymilvus 2.4.6 / pytest

## Global Constraints

- **测试必须用真实 PostgreSQL + 完整 API 链路，禁止 sqlite**（`Harness/rules/测试规范.md`）。
- 集成测试需要 `TEST_DATABASE_URL`，缺失时应 fail-fast。
- 集成测试运行命令：
  `cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest <path> -q`
- **Alembic 默认打 prod**（`alembic/env.py` 只认 `DATABASE_URL`，默认 5432/prod）；本计划**不产生任何 PG 迁移**。
- 本计划**包含一次 Milvus 集合重建**。执行前必须复查 `document_embeddings` 仍为空。
- 文件 200-400 行宜，上限 800；函数 < 50 行；嵌套 ≤ 4 层。
- 不可变数据：返回新对象，禁止原地修改。
- **错误不得静默吞掉**：本计划修掉一处既有的 `except Exception: pass`（它掩盖了真实故障，见 Task 5）。
- 注释与 docstring 用中文（与代码库一致）。
- Conventional Commits。
- **源文件字节暂不存储（范围约束，用户 2026-09-12 决定）**：只保存**解析后的内容**（Milvus chunk + `wiki_page.content`），保证可检索、可供 aichat 使用，并可通过 `source_ref` 回溯到本地来源。`document_catalog.storage_url` 写的是**预留键**——`s3://qa-knowledge-sources/sources/<hash>/<name>`，指向一个**当前尚不存在**的对象；`content_hash` 是真实摘要。后续要真正存储时，按同一预留键 PUT 即可，**无需回填、无需迁移**。
  - **消费者不得假定该 URL 现在可读。** 它不是可下载链接，是「将来会放在这里」的契约。
  - 本计划**不引入 `minio` 依赖、不新增容器、不新增备份脚本** —— YAGNI：存储能力尚未启用，先不建。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/app/services/document_parser.py` | 改 | 新增 `TextBlock`；`parse_document` 返回 `list[TextBlock]` |
| `backend/app/services/chunk_splitter.py` | 改 | `split_by_paragraphs` 吃 `list[TextBlock]`；定位符写进 `Chunk.metadata` |
| `backend/app/infrastructure/milvus_client.py` | 改 | `_documentFields()` 加 3 个定位符字段；`insertDocumentChunks` 对应加列 |
| `backend/scripts/rebuild_document_collection.py` | 建 | 集合重建（`_ensureCollection` 已存在即早返回，改字段不重建不生效） |
| `backend/app/infrastructure/source_locator.py` | 建 | 纯函数：`hashBytes` / `hashText` / `buildSourceObjectName` / `buildStorageUrl`。算预留存储键，**零 I/O、零依赖** |
| `backend/app/services/rag_service.py` | 改 | 写预留 `storage_url` + 真实 `content_hash`；修 `except: pass` 与漏传 actor |
| `backend/app/services/wiki_import_service.py` | 改 | `parseFile` 适配新返回类型；`execute` 登记 `document_catalog` |
| `backend/app/tests/unit/test_document_parser.py` | 改 | 断言定位符 |
| `backend/app/tests/unit/test_chunk_splitter.py` | 改 | 签名变更 + 定位符断言 |
| `backend/app/tests/unit/test_rag_service.py` | 改 | mock 适配新返回类型 |
| `backend/app/tests/unit/test_source_locator.py` | 建 | 预留键与摘要的纯函数单测 |
| `backend/app/tests/integration/test_rag_ingest_provenance.py` | 建 | 端到端：上传 → Milvus 带定位符 → catalog 有预留 url + 真实 hash |
| `backend/scripts/wiki_provenance_realdata.py` | 建 | 真实数据验证（Harness 开发门禁） |
| `Harness/changes/feat-wiki-provenance/summary.md` | 建 | 九段变更记录（SSOT） |

> **不动的文件（相对原设计的删减）**：`backend/pyproject.toml` / `uv.lock`（不装 `minio`）、
> `docker/docker-compose.yml`（不加 `qa-objects` 服务与 `objects_data` 卷）、
> `scripts/backup_objects.sh`（无对象存储可备份）、`Harness/rules/数据存储防护.md`（无新卷）。

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
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for text in pageTexts:
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
cd backend && .venv/bin/pytest app/tests/unit/test_document_parser.py -q
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
cd backend && .venv/bin/pytest app/tests/unit/test_document_parser.py -q
```

Expected: PASS（11 个用例）

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
> **跨块 chunk 的定位符取首块**：一个 chunk 若跨越多个 TextBlock，其定位符取**第一个块**（chunk 的起点）。这是有意选择，需在 docstring 写明。

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

    def test_chunk_spanning_blocks_takes_first_locator(self) -> None:
        blocks = [_block("甲", page=1, para=1), _block("乙", page=1, para=2)]
        chunks = split_by_paragraphs(blocks, chunk_size=500)
        assert len(chunks) == 1
        assert chunks[0].metadata["paragraph_no"] == 1

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
cd backend && .venv/bin/pytest app/tests/unit/test_chunk_splitter.py -q
```

Expected: FAIL — `TypeError: 'TextBlock' object is not subscriptable`（旧实现 `text.split("\n")` 收到列表）

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
cd backend && .venv/bin/pytest app/tests/unit/test_chunk_splitter.py -q
```

Expected: PASS（14 个用例）

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

> **Milvus 标量字段不可为 NULL**（2.4.x 无 nullable 标量）。空值用哨兵：`page_number` / `paragraph_no` 用 `-1`，`section_name` 用 `""`。这是无损的——`-1` 不是合法页码/段号。
>
> **`_ensureCollection` 已存在即早返回**（`milvus_client.py:104-113`），所以**只改字段不重建不生效**。且 `CollectionSchema` **未设 `enable_dynamic_field`**（默认 False），无法靠动态字段绕过。本计划执行前已确认该集合为空，重建零成本——**执行前必须再验一次**：
>
> ```bash
> docker exec qa-milvus python -c "print('ok')" 2>/dev/null
> # 或经后端容器：
> docker exec qa-backend python -c "
> from pymilvus import Collection, utility, connections
> connections.connect(alias='d', host='milvus', port='19530')
> print('rows =', Collection('document_embeddings', using='d').num_entities)"
> ```
> 若 `rows > 0`，**停止**并先与用户确认数据处置方式。

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
    ensureDocumentCollection,
    insertDocumentChunks,
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

- [ ] **Step 6: 建重建脚本**

新建 `backend/scripts/rebuild_document_collection.py`：

```python
"""重建 Milvus document_embeddings 集合。

为什么需要它：``_ensureCollection`` 见到集合已存在就早返回，因此**改字段后
不重建不生效**；且 CollectionSchema 未开 enable_dynamic_field，无法绕过。

安全闸：集合非空时拒绝执行，除非显式设 ``ALLOW_NONEMPTY_REBUILD=1``。

用法：
    docker exec qa-backend python scripts/rebuild_document_collection.py
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
docker exec qa-backend python scripts/rebuild_document_collection.py
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/integration/test_milvus_document_fields.py -q
```

Expected: 重建脚本输出「已重建集合」；测试 PASS（4 个用例）

- [ ] **Step 8: 提交**

```bash
git add backend/app/infrastructure/milvus_client.py backend/scripts/rebuild_document_collection.py \
        backend/app/tests/integration/test_milvus_document_fields.py
git commit -m "feat(wiki): Milvus 文档集合加定位符字段 + 集合重建脚本"
```

---

## Task 4: 源文件存储占位（`source_locator` 纯函数）

**Files:**
- Create: `backend/app/infrastructure/source_locator.py`
- Test: `backend/app/tests/unit/test_source_locator.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `DEFAULT_BUCKET = "qa-knowledge-sources"`
  - `def hashBytes(content: bytes) -> str` —— sha256 十六进制小写（64 位）
  - `def hashText(text: str) -> str` —— `hashBytes(text.encode("utf-8"))`，与 P1 的 `contentHashOf` **同口径**
  - `def buildSourceObjectName(contentHash: str, filename: str) -> str` —— `sources/<hash[:2]>/<hash>/<safeName>`
  - `def buildStorageUrl(objectName: str, bucket: str = DEFAULT_BUCKET) -> str` —— `s3://<bucket>/<objectName>`

> ### 为什么是「占位」而不是「存储」
>
> 用户 2026-09-12 决定：**上传的源文档暂时不存储**，但要**预留可以存储到指定位置的占位**，后续可以存储并查看；**只保存解析的内容**，保证可检索、可供 aichat 使用，并**可以与本地关联**。
>
> 因此本任务**只算键、不写字节**：
>
> - **零 I/O、零新依赖、零新容器** —— 这也是本计划仍可用 `scripts/deploy_backend.sh` 部署的原因（见 Task 7）。
> - **占位键现在就算好并落库**：后续补传源文件时落在同一个 key，**不需要回填、不需要迁移**。这是「预留」的全部意义 —— 若现在写 `None`，将来补传就必须回填历史行。
> - **内容寻址** `sources/<hash[:2]>/<hash>/<name>`：同一内容落同一 key，后续补传天然去重；与 P1 的 `page_id` 内容派生同一思路。
> - **与本地关联**：`wiki_import_task.source_ref`（用户在导入向导填的本地来源名）+ `page_ids` 已构成完整回溯链，**本任务不新增列、不改 schema**。后续真要补传源文件时，正是靠这条链找到本地那份文件。
> - **消费者不得假定 `storage_url` 现在可读** —— 它是「将来会放在这里」的契约，不是下载链接。
>
> **摘要口径**：`hashText` 必须与 P1 的 `contentHashOf` 逐字节一致（`sha256(utf-8).hexdigest()`）。见文末「跨计划协调」第 2 节。

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/unit/test_source_locator.py`：

```python
"""源文件预留存储键与摘要的纯函数单测（P0 溯源地基）。

本模块**不做任何 I/O** —— 用户决定源文件字节暂不存储，只预留位置。
因此这里没有 mock，全是确定性断言。
"""

from __future__ import annotations

import hashlib

from app.infrastructure.source_locator import (
    DEFAULT_BUCKET,
    buildSourceObjectName,
    buildStorageUrl,
    hashBytes,
    hashText,
)


class TestHashBytes:
    def test_sha256_known_value(self) -> None:
        # echo -n "" | sha256sum
        assert hashBytes(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_is_lowercase_hex_64(self) -> None:
        h = hashBytes(b"abc")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_content_same_hash(self) -> None:
        assert hashBytes(b"abc") == hashBytes(b"abc")

    def test_different_content_different_hash(self) -> None:
        assert hashBytes(b"abc") != hashBytes(b"abd")


class TestHashText:
    def test_matches_utf8_bytes_digest(self) -> None:
        """必须按 UTF-8 编码取字节 —— 中文不能走 locale 编码。"""
        text = "供应商准入规则：注册资本 >= 1000 万"
        assert hashText(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_known_value_for_ascii(self) -> None:
        # echo -n "abc" | sha256sum
        assert hashText("abc") == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )

    def test_text_and_bytes_agree(self) -> None:
        """同口径：hashText(s) 必须等于 hashBytes(s.encode("utf-8"))。"""
        assert hashText("中文") == hashBytes("中文".encode("utf-8"))


class TestBuildSourceObjectName:
    def test_content_addressed_layout(self) -> None:
        h = hashBytes(b"abc")
        assert buildSourceObjectName(h, "制度.pdf") == f"sources/{h[:2]}/{h}/制度.pdf"

    def test_same_hash_different_filename_differs(self) -> None:
        h = hashBytes(b"abc")
        assert buildSourceObjectName(h, "a.pdf") != buildSourceObjectName(h, "b.pdf")

    def test_path_traversal_in_filename_is_neutralized(self) -> None:
        h = hashBytes(b"abc")
        name = buildSourceObjectName(h, "../../etc/passwd")
        assert ".." not in name
        assert "/etc/" not in name
        assert name.startswith(f"sources/{h[:2]}/{h}/")

    def test_empty_filename_falls_back(self) -> None:
        h = hashBytes(b"abc")
        assert buildSourceObjectName(h, "") == f"sources/{h[:2]}/{h}/unnamed"


class TestBuildStorageUrl:
    def test_default_bucket(self) -> None:
        assert buildStorageUrl("sources/aa/x/a.pdf") == (
            "s3://qa-knowledge-sources/sources/aa/x/a.pdf"
        )

    def test_custom_bucket(self) -> None:
        assert buildStorageUrl("sources/aa/x/a.pdf", bucket="b1") == (
            "s3://b1/sources/aa/x/a.pdf"
        )

    def test_is_deterministic(self) -> None:
        assert buildStorageUrl("k") == buildStorageUrl("k")

    def test_does_not_touch_network(self) -> None:
        """纯函数：构造 URL 不产生任何 I/O（无 mock 也不该抛错）。"""
        assert buildStorageUrl("k").startswith(f"s3://{DEFAULT_BUCKET}/")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/pytest app/tests/unit/test_source_locator.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.infrastructure.source_locator'`

- [ ] **Step 3: 实现**

新建 `backend/app/infrastructure/source_locator.py`：

```python
"""源文件的预留存储位置与内容摘要（P0 溯源地基）。

**本模块不做 I/O。** 用户 2026-09-12 决定：上传的源文档暂时不存储，
但预留一个确定性的存储位置占位，后续可以存储并查看；只保存解析后的
内容，保证可检索、可供 aichat 使用，并能与本地来源关联。

因此这里只算「键」：
  - ``hashBytes`` / ``hashText``  —— 内容摘要（与 P1 的 contentHashOf 同口径）
  - ``buildSourceObjectName``     —— 内容寻址的对象名
  - ``buildStorageUrl``           —— 预留的 s3:// 位置

占位键现在算好并落库，后续补传源文件时落在同一个 key，无需回填、无需迁移。
``document_catalog.storage_url`` 因此**当前不可读** —— 它是契约，不是下载链接。
"""

from __future__ import annotations

import hashlib
import re

DEFAULT_BUCKET = "qa-knowledge-sources"

# 文件名里可能带路径分隔符或 ..，落到对象名上会越权，统一净化。
# 允许：ASCII 字母数字、点、下划线、连字符、CJK 统一表意文字（U+4E00–U+9FFF）。
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._一-鿿-]")


def hashBytes(content: bytes) -> str:
    """字节内容的 SHA-256（十六进制小写，64 位）。"""
    return hashlib.sha256(content).hexdigest()


def hashText(text: str) -> str:
    """文本内容的 SHA-256（按 UTF-8 编码取字节后摘要）。

    与 ``hashBytes(text.encode("utf-8"))`` 逐字节等价，也与 P1 的
    ``contentHashOf`` 同口径 —— 两份计划不得各自漂移（见跨计划协调第 2 节）。
    """
    return hashBytes(text.encode("utf-8"))


def buildSourceObjectName(contentHash: str, filename: str) -> str:
    """由内容哈希 + 文件名派生对象名（内容寻址）。

    同一内容落同一 key，后续补传天然去重。
    """
    safeName = _UNSAFE_NAME_RE.sub("_", filename.rsplit("/", 1)[-1]).strip("_")
    if not safeName:
        safeName = "unnamed"
    return f"sources/{contentHash[:2]}/{contentHash}/{safeName}"


def buildStorageUrl(objectName: str, bucket: str = DEFAULT_BUCKET) -> str:
    """构造**预留**的存储位置（当前无对应对象）。

    消费者不得假定其可读 —— 它标记「将来会放在这里」。
    """
    return f"s3://{bucket}/{objectName}"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/bin/pytest app/tests/unit/test_source_locator.py -q
```

Expected: PASS（15 个用例）

- [ ] **Step 5: 提交**

```bash
git add backend/app/infrastructure/source_locator.py backend/app/tests/unit/test_source_locator.py
git commit -m "feat(wiki): source_locator 纯函数 —— 源文件预留存储键与内容摘要"
```

---

## Task 5: `rag_service` 写预留 `storage_url` 与真实 `content_hash`，消除静默失败

**Files:**
- Modify: `backend/app/services/rag_service.py:94-180`
- Test: `backend/app/tests/unit/test_rag_service.py`

**Interfaces:**
- Consumes: `TextBlock` / `split_by_paragraphs(list[TextBlock])`（Task 1/2）、`milvus insertDocumentChunks` 新字段（Task 3）、`source_locator`（Task 4）
- Produces: `RagService.ingestDocument(...)` 行为变更 —— `document_catalog` 记**预留** `storage_url` + 真实 `content_hash`；返回值新增 `storage_url` / `content_hash` 两键

> ### 本任务顺手修掉两个既有缺陷
>
> 与存储决定无关，是读码时发现的真 bug：
>
> 1. **`updateDocument` 漏传 `actor`**：`document_service.py:145` 的签名是
>    `updateDocument(self, session, id, dto, actor)` —— 四个参数，而 `rag_service.py:174`
>    只传三个 → `TypeError`。该异常随即被下面的 `except Exception: pass` 吞掉，所以
>    **「文档已存在」这个分支在生产里从未真正生效过**（每次重传同一文档都静默丢弃元数据更新）。
> 2. **`except Exception: pass`**：删除，让异常冒泡为 500 并在日志留下上下文。
>
> **行为变更（有意）**：这会改变一条既有测试的期望，属预期。
>
> ### `storage_url` 语义变更
>
> 从假 URL `milvus://N_chunks` 改为 **Task 4 算出的预留 s3 键**。注意它**当前不可读** ——
> 源文件字节按用户决定暂不存储。这是一个明确的、将来会兑现的契约，好过一个看起来像
> URL 的假值。消费者不得假定可下载。

- [ ] **Step 1: 改测试**

修改 `backend/app/tests/unit/test_rag_service.py`。找到 patch `parse_document` 返回字符串的
两处（约 `:229-237` 与 `:308-316`），把 mock 改成返回 `TextBlock` 列表，并给
`split_by_paragraphs` 的 mock chunk 补 `metadata`：

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
```

> **不再 patch `putSourceObject`** —— Task 4 的模块是纯函数，没有 I/O 可 mock。
> 断言改为直接对**算出来的预留键**取值（用 `hashlib` 独立算出，不调 `source_locator`）。

文件顶部补 import：

```python
import hashlib

from app.services.document_parser import TextBlock
```

再加 `_mockChunk` 辅助与三条用例。**注意裸 `MagicMock` 的 `.metadata.get(...)` 会返回一个 MagicMock**，
直接写进 Milvus records 语义不明，所以用 `_mockChunk` 显式给定 metadata：

```python
def _mockChunk(chunkId: str, text: str, seq: int, *, page: int | None = 1) -> MagicMock:
    """构造带定位符 metadata 的假 chunk。"""
    return MagicMock(
        chunk_id=chunkId,
        text=text,
        sequence=seq,
        metadata={"page_number": page, "section_name": None, "paragraph_no": seq + 1},
    )


def _expectedReservedUrl(content: bytes, filename: str) -> str:
    """独立算出预留键（只依赖标准库，不复用 source_locator，避免自证）。"""
    h = hashlib.sha256(content).hexdigest()
    return f"s3://qa-knowledge-sources/sources/{h[:2]}/{h}/{filename}"
```

```python
    @pytest.mark.asyncio
    async def test_ingest_records_reserved_url_and_real_hash(self) -> None:
        """catalog 必须记预留 s3 键 + 真实 hash，不再写假 milvus:// URL。"""
        # Arrange
        mock_session = MagicMock()
        mock_emb = AsyncMock()
        mock_emb.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)
        mock_blocks = [
            TextBlock(
                text="这是测试文档内容。", page_number=18, section_name="质量管理", paragraph_no=3
            )
        ]
        mock_chunks = [_mockChunk("chunk-0", "这是测试文档内容。", 0, page=18)]
        expectedUrl = _expectedReservedUrl(b"dummy", "test.txt")

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

        # Assert：返回值带预留 url + 真实 hash
        assert result["storage_url"] == expectedUrl
        # sha256(b"dummy")，钉死字面量以防摘要口径漂移
        assert result["content_hash"] == (
            "b5a2c96250612366ea272ffac6d9744aaf4b45aacd96aa7cfcb931ee3b558259"
        )

        # Assert：落库的 document 元数据是真值
        createdDto = svc._doc_svc.createDocument.await_args.kwargs["dto"]
        assert createdDto.storage_url == expectedUrl
        assert createdDto.content_hash == result["content_hash"]
        # 不得再出现假 URL 的形状
        assert not createdDto.storage_url.startswith("milvus://")

        # Assert：定位符进了 Milvus 记录
        record = mock_insert.call_args.args[0][0]
        assert record["page_number"] == 18
        assert record["section_name"] == "质量管理"
        assert record["paragraph_no"] == 3

    @pytest.mark.asyncio
    async def test_reserved_url_is_deterministic_across_calls(self) -> None:
        """同一文件重传必须算出同一个预留键 —— 否则「后续补传」会落到两个位置。"""
        # Arrange
        mock_session = MagicMock()
        mock_emb = AsyncMock()
        mock_emb.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)
        blocks = [TextBlock(text="内容", page_number=1, section_name=None, paragraph_no=1)]
        chunks = [_mockChunk("chunk-0", "内容", 0)]
        urls: list[str] = []

        # Act：连续两次同输入
        for _ in range(2):
            with patch(
                "app.services.rag_service.parse_document",
                new_callable=AsyncMock,
                return_value=blocks,
            ), patch(
                "app.services.rag_service.split_by_paragraphs", return_value=chunks
            ), patch(
                "app.services.rag_service._getEmbeddingService", return_value=mock_emb
            ), patch(
                "app.services.rag_service.insertDocumentChunks"
            ):
                svc = RagService()
                svc._doc_svc = self._mock_doc_svc_with_create()
                res = await svc.ingestDocument(
                    mock_session,
                    content=b"dummy",
                    filename="test.txt",
                    mime_type="text/plain",
                    actor=CurrentUser(userId="test-user"),
                )
                urls.append(res["storage_url"])

        # Assert
        assert urls[0] == urls[1]

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

        # Assert：走的是 updateDocument，且写的是预留 URL
        docSvc.updateDocument.assert_awaited()
        updateDto = docSvc.updateDocument.await_args.args[2]
        assert updateDto.storage_url == _expectedReservedUrl(b"dummy", "test.txt")
        assert updateDto.content_hash == (
            "b5a2c96250612366ea272ffac6d9744aaf4b45aacd96aa7cfcb931ee3b558259"
        )
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

> `updateDocument(session, id, dto, actor=actor)` 的位置参数顺序若与 `args[2]` 不符，
> 以 `rag_service.py` 里的实际调用为准调整。
> 同文件其他用例（Milvus 失败、embedding 失败等）里的 `parse_document` mock 与
> `split_by_paragraphs` 返回值**全部**要按上面的形状改（`list[TextBlock]` + 带 metadata 的 chunk），
> 否则签名变更会让它们一起挂掉。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/pytest app/tests/unit/test_rag_service.py -q
```

Expected: FAIL — `TypeError: object of type 'list' has no len()` / `AttributeError: 'list' object has no attribute 'strip'`

- [ ] **Step 3: 实现**

修改 `backend/app/services/rag_service.py`。顶部 import 补：

```python
from app.infrastructure.source_locator import (
    buildSourceObjectName,
    buildStorageUrl,
    hashBytes,
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

        # 2. 源文件预留存储位置（纯计算，不写字节）
        #    用户决定源文件暂不存储，但把位置现在就算好并落库 ——
        #    后续补传落在同一个 key，无需回填、无需迁移。
        contentHash = hashBytes(content)
        storageUrl = buildStorageUrl(buildSourceObjectName(contentHash, filename))

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
            # 已存在：刷新预留位置与摘要。
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

（**删除** 原来的 `try/except Exception: pass`。让异常冒泡为 500 并在日志中留下上下文，
胜过一次静默的元数据丢失。已核对该分支的调用签名：`document_service.py:145` 的
`updateDocument(self, session, id, dto, actor)`。）

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
cd backend && .venv/bin/pytest app/tests/unit/test_rag_service.py -q
```

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/rag_service.py backend/app/tests/unit/test_rag_service.py
git commit -m "fix(wiki): rag_service 写预留 storage_url + 真实 content_hash，修漏传 actor 与静默吞异常"
```

---

## Task 6: Wiki 导入路径登记 `document_catalog`

**Files:**
- Modify: `backend/app/services/wiki_import_service.py`
- Test: `backend/app/tests/integration/test_wiki_import_catalog.py`

**Interfaces:**
- Consumes: `parse_document` 新返回类型（Task 1）、`source_locator`（Task 4）
- Produces: `WikiImportService.execute(...)` 在每个成功批次末尾登记/更新一条 `document_catalog`

> ### ⚠️ Spec 修正（必须知悉）
>
> Spec §4.1 的 **C5 写作「Wiki 导入路径也注册 `document_catalog`」**，暗示在 `execute` 里挂上进目录登记即可。**读码后发现这不可直接实现**：
>
> - `parse_document` 在 wiki 路径下只有一个调用点——`wiki_import_service.py:205`，位于 `parseFile()`，即**预览路径**，其 docstring 明确写「不落库、不调模型」。
> - `execute()` 的输入是 `dto.drafts`（客户端传来的**纯文本草稿**），**从不接触原始文件字节**。因此 `execute` 拿不到 `content`，无法算文件哈希、无法存源文件。
>
> 本任务实现**最接近原意且不破坏「预览不落库」不变量**的版本：
>
> - 在 `execute` 里以**草稿文本的哈希**作为 `content_hash`，用 Task 4 的 `hashText`。
> - `storage_url` 写 **Task 4 算出的预留位置**（由草稿哈希 + `source_ref` 派生），**不是 `None`** ——
>   用户 2026-09-12 决定「预留可以存储到指定位置的占位，后续可以存储并查看」。写 `None` 会让
>   将来的补传必须回填历史行；写占位键则一次算好、永久有效。
>
> **已确认的范围决定（用户 2026-09-12）**：上传的源文档**暂时不存储**，只保存解析的内容，
> 保证可检索、可供 aichat 使用，并可与本地关联。因此**不改造** preview→execute 契约、
> **不引入**上传令牌 —— 那是存储能力真正启用时的事，本计划只把位置预留好。
>
> ### ⚠️ 已知局限（必须写进变更记录）
>
> wiki 路径的 `content_hash` 是**草稿文本**的摘要，不是原始文件的摘要（`execute` 拿不到文件字节）。
> 因此同一份文件若既走 wiki 导入、又走 RAG 上传，会在 `document_catalog` 里产生**两行**、
> 两个不同的 `content_hash`。这是路径差异，不是 bug；但 P1 建在 `content_hash` 上的唯一索引
> **不会**把这两行判为重复，运维排查时须知悉。

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/integration/test_wiki_import_catalog.py`。fixture 与假 LLM 客户端的写法**照抄** `test_wiki_import_api.py`（同目录，已跑通）：

```python
"""Wiki 导入路径登记 document_catalog（P0 溯源地基）。

真实 PostgreSQL + 完整 API 链路。LLM 走 patch 注入假客户端——不联外网，
但保留真实调用链（ModelConfigService 查配置 → createClient → complete）。
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DocumentType
from app.domain.models import DocumentCatalog, LlmConfig
from app.infrastructure.llm.base_client import LlmResponse

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/import"
_INVOKER_CLIENT = "app.services.learning.llm_invoker.createClient"

_CLASSIFY_JSON = json.dumps(
    {
        "primary": "RULE",
        "confidence": 0.92,
        "alternatives": ["POLICY"],
        "reason": "含准入门槛阈值",
    }
)


class _FakeLlmClient:
    """假 LLM 客户端（与 test_wiki_import_api.py 同一形状）。"""

    def __init__(self, content: str = _CLASSIFY_JSON) -> None:
        self._content = content

    async def complete(self, messages, **kwargs) -> LlmResponse:
        return LlmResponse(
            content=self._content,
            modelName="fake-model",
            promptTokens=100,
            completionTokens=50,
        )


async def _seedModel(
    dbSession: AsyncSession,
    *,
    modelName: str = "catalog-test-model",
) -> int:
    """插入一条 llm_config，返回其 id（字段名照抄 test_wiki_import_api.py）。"""
    config = LlmConfig(
        model_name=modelName,
        provider="openai_compatible_proxy",
        is_active=True,
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config.id


def _drafts(*items: tuple[str, str]) -> list[dict]:
    return [{"title": t, "content": c} for t, c in items]


async def _executeImport(client: AsyncClient, modelId: int, sourceRef: str = "policy.md"):
    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        return await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": _drafts(("供应商准入规则", "注册资本 >= 1000 万")),
                "modelId": modelId,
                "sourceType": "MARKDOWN",
                "sourceRef": sourceRef,
            },
        )


async def test_execute_registers_document_catalog(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """导入执行后 document_catalog 新增一行，content_hash 为 64 位 hex。"""
    # Arrange
    modelId = await _seedModel(dbSession)

    # Act
    resp = await _executeImport(client, modelId)

    # Assert
    assert resp.status_code == 201
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.document_name == "policy.md"
    assert row.content_hash is not None
    assert len(row.content_hash) == 64
    assert all(c in "0123456789abcdef" for c in row.content_hash)
    # storage_url 必须是**预留键**：既不是假 URL，也不是 None。
    # 写 None 会让将来的补传必须回填历史行（见 Task 6 的范围决定）。
    assert row.storage_url.startswith("s3://qa-knowledge-sources/sources/")
    assert row.storage_url.endswith("/policy.md")
    assert "milvus://" not in row.storage_url
    # 不能是 dto.source_type（"MARKDOWN"）——那既不是合法枚举值也语义不符
    assert row.document_type == DocumentType.OTHER


async def test_reimport_same_drafts_does_not_duplicate_catalog(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同一批草稿重复导入，catalog 不应新增第二行。"""
    # Arrange
    modelId = await _seedModel(dbSession)

    # Act
    first = await _executeImport(client, modelId)
    second = await _executeImport(client, modelId)

    # Assert
    assert first.status_code == 201
    assert second.status_code == 201
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert len(rows) == 1, f"同 hash 重复导入产生了 {len(rows)} 行 catalog"


async def test_different_drafts_produce_separate_catalog_rows(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """不同内容的导入应是两行 —— 否则上面的去重可能是「永远只写一行」的假象。"""
    # Arrange
    modelId = await _seedModel(dbSession)

    # Act
    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        respA = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": _drafts(("准入规则", "注册资本 >= 1000 万")),
                "modelId": modelId,
                "sourceType": "MARKDOWN",
                "sourceRef": "a.md",
            },
        )
        respB = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": _drafts(("验收标准", "抽检合格率 >= 98%")),
                "modelId": modelId,
                "sourceType": "MARKDOWN",
                "sourceRef": "b.md",
            },
        )

    # Assert
    assert respA.status_code == 201
    assert respB.status_code == 201
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert len(rows) == 2
    assert {r.document_name for r in rows} == {"a.md", "b.md"}


async def test_preview_file_still_returns_flat_text(client: AsyncClient) -> None:
    """parseFile 仍返回扁平文本供预览展示（由块列表拼回），接口语义不变。"""
    # Act
    resp = await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("规则.txt", "## 准入规则\n\n注册资本 >= 1000 万。", "text/plain")},
    )

    # Assert
    assert resp.status_code == 200
    body = resp.json()
    assert "注册资本 >= 1000 万。" in body["text"]
    assert body["sourceType"] == "MARKDOWN"
```

> 四个用例覆盖：登记发生、同内容不重复、**不同内容确实产生两行**、预览路径未被破坏。第三条是必须的——只有前两条时，一个「永远只写一行」的实现也能全绿。
>
> 端点路径是 `/preview-file`（连字符，非 `/preview/file`），响应键是 `text` / `sourceType`。以上均取自 `test_wiki_import_api.py` 实际用例，非推测。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/integration/test_wiki_import_catalog.py -q
```

Expected: FAIL — catalog 行数为 0

- [ ] **Step 3: 适配 `parseFile`**

`wiki_import_service.py:205` 改为：

```python
        blocks = await parse_document(content, mime_type, filename)
        # 预览仍要扁平文本（前端按段落编辑），由带定位的块拼回。
        text = "\n\n".join(b.text for b in blocks)
        return text, sourceTypeFromFilename(filename, mime_type)
```

- [ ] **Step 4: 在 `execute` 末尾登记 catalog**

在 `execute` 收尾处（任务台账落库之后）追加：

`wiki_import_service.py` 顶部 import 补：

```python
from app.infrastructure.source_locator import (
    buildSourceObjectName,
    buildStorageUrl,
    hashText,
)
```

```python
        # P0 溯源：登记 document_catalog，使 wiki 摄入来源可被检索与去重。
        # execute 只持有草稿文本、不持有原始文件，所以：
        #   - content_hash 是草稿文本的摘要（对去重与变更检测同样有效）
        #   - storage_url 是**预留**位置：源文件按用户决定暂不存储，
        #     但位置现在就算好，将来补传落在同一个 key，无需回填、无需迁移。
        draftText = "\n\n".join(d.content for d in dto.drafts if d.content)
        if draftText:
            contentHash = hashText(draftText)
            reservedName = buildSourceObjectName(contentHash, dto.source_ref or "unnamed")
            await self._catalog.upsertByContentHash(
                session,
                document_name=dto.source_ref or "未命名导入",
                content_hash=contentHash,
                storage_url=buildStorageUrl(reservedName),
                actor=actor,
            )
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
```

新建 `backend/app/services/wiki_catalog_registrar.py`（保持 `wiki_import_service.py` 不膨胀）：

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
        不属于 CONTRACT / SOP 等既有文档类别。**不能**透传 dto.source_type
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

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/integration/test_wiki_import_catalog.py -q
```

Expected: PASS

- [ ] **Step 6: 回归既有 wiki 导入测试**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/ -q -k "wiki_import"
```

Expected: PASS（`parseFile` 返回值语义未变，既有预览测试应全绿）

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/wiki_import_service.py backend/app/services/wiki_catalog_registrar.py \
        backend/app/tests/integration/test_wiki_import_catalog.py
git commit -m "feat(wiki): wiki 导入路径登记 document_catalog + parseFile 适配 TextBlock"
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

> **部署方式不变（好消息）**：本计划**没有新增任何 Python 依赖** —— `pypdf`、`python-docx`、
> `reportlab` 早已在 `pyproject.toml` 里。所以照常走 `./scripts/deploy_backend.sh`：它会
> `docker cp` **`app/` + `scripts/` + `alembic/` 三处**（三者必须一起灌，否则容器 CMD 的
> `alembic upgrade head` 找不到 rev，uvicorn 起不来）。**不需要重建镜像。**

- [ ] **Step 1: 写真实数据验证脚本**

新建 `backend/scripts/wiki_provenance_realdata.py`：

```python
"""P0 溯源地基真实数据验证（Harness 开发门禁）。

对一个真实 PDF 走解析 → 分块 → 预留键计算链路，断言：
  1. 分块后的 chunk 带正确 page_number
  2. 预留 storage_url 形状正确且**确定性**（同输入同输出）
  3. content_hash 是 64 位小写十六进制

**注意**：本脚本**不读写任何对象存储** —— 源文件字节按用户 2026-09-12 的决定
暂不存储，只预留位置。所以这里没有上传/读回断言，那是存储能力启用后才有的事。

用法：
    docker exec qa-backend python scripts/wiki_provenance_realdata.py
"""

from __future__ import annotations

import asyncio
import io
import re

from app.infrastructure.source_locator import (
    buildSourceObjectName,
    buildStorageUrl,
    hashBytes,
)
from app.services.chunk_splitter import split_by_paragraphs
from app.services.document_parser import parse_document

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


async def main() -> int:
    failures: list[str] = []

    # --- 1. 解析：定位符必须存在 ---
    content = _samplePdfBytes()
    blocks = await parse_document(content, "application/pdf", "provenance-sample.pdf")
    if not blocks:
        failures.append("解析结果为空")
    elif any(b.page_number is None for b in blocks):
        failures.append(
            f"存在无页码的块：{[b.text[:20] for b in blocks if b.page_number is None]}"
        )

    # --- 2. 分块：定位符必须透传 ---
    chunks = split_by_paragraphs(blocks)
    if not chunks:
        failures.append("分块结果为空")
    elif any(c.metadata.get("page_number") is None for c in chunks):
        failures.append("存在无页码的 chunk")

    # --- 3. 预留键：形状正确且确定性 ---
    h = hashBytes(content)
    recomputed = buildStorageUrl(
        buildSourceObjectName(hashBytes(content), "provenance-sample.pdf")
    )
    url = buildStorageUrl(buildSourceObjectName(h, "provenance-sample.pdf"))
    if not _HASH_RE.match(h):
        failures.append(f"content_hash 形状不对：{h}")
    if not url.startswith("s3://qa-knowledge-sources/sources/"):
        failures.append(f"storage_url 形状不对：{url}")
    if url != recomputed:
        failures.append("预留键不确定：同输入产出不同 URL")
    if "/../" in url:
        failures.append(f"预留键含越权路径段：{url}")

    print(f"page_number 序列: {[c.metadata['page_number'] for c in chunks]}")
    print(f"content_hash:     {h}")
    print(f"storage_url:      {url}  （预留位置，当前无对应对象）")

    if failures:
        print("\n❌ 失败项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n✅ P0 溯源地基真实数据验证通过")
    return 0


def _samplePdfBytes() -> bytes:
    """用 reportlab 生成两页 PDF（与 test_document_parser 的 _makePdf 同源）。

    不用手搓 PDF 字节：xref 偏移量极易写错，而且写错之后 pypdf 读到的是
    **0 页**而非报错 —— 脚本会以「空结果」的形式静默通过。
    reportlab 是已声明依赖（`reportlab>=4.2.0`，实机 5.0.0，已验证可导入）。
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for text in ("第一页：供应商准入需注册资本 >= 1000 万", "第二页：质量协议每年复核一次"):
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

> 该辅助**有意与 `backend/app/tests/unit/test_document_parser.py` 的 `_makePdf` 重复** ——
> 脚本跨目录 import 测试夹具会引入不必要的耦合。

- [ ] **Step 2: 跑验证脚本**

```bash
docker exec qa-backend python scripts/wiki_provenance_realdata.py
```

Expected: 输出页码序列 / hash / 预留 URL，末尾 `✅ P0 溯源地基真实数据验证通过`

- [ ] **Step 3: 部署**

```bash
./scripts/deploy_backend.sh
docker exec qa-backend alembic current    # 本计划无新迁移，应停在既有 head
docker exec qa-backend python scripts/rebuild_document_collection.py
```

Expected: 脚本报告灌入 `app/`+`scripts/`+`alembic/` 三处成功且容器健康；集合重建成功

> **集合重建是本计划唯一不可逆的操作**。执行前用 Task 3 的前置检查确认
> `document_embeddings` 仍为空 —— 空集合重建无数据损失。

- [ ] **Step 4: 验证回滚路径**

```bash
docker exec qa-backend alembic current   # 记下版本号，回滚后应一致（本计划无迁移）
./scripts/deploy_backend.sh --rollback   # 从快照目录还原上一版代码
```

> P0 无 PG 迁移，回滚不涉及数据。**唯一不可逆项**是 Milvus 集合重建 —— 但重建前该集合为空，
> 故无数据损失。`document_catalog` 里的预留 URL 是**纯计算结果、无外部状态**，回滚后仍指向
> 同一个位置，所以回滚不会让「将来的补传」失效。

- [ ] **Step 5: 写九段变更记录**

新建 `Harness/changes/feat-wiki-provenance/summary.md`，按 `Harness/changes/_template/summary.md`
的九段结构填写。**第 9 段「真实数据验证报告」必须包含 Step 2 的完整实际输出** —— 照抄终端输出，
不得概括。同时必须记录：

- **范围决定**：源文件字节暂不存储，只预留位置（用户 2026-09-12 决定）。必须写明
  `document_catalog.storage_url` **当前不可读、不是下载链接**，以及后续补传落在同一 key、
  无需回填。同时写明**删减项**：未引入 `minio` 依赖、未加容器、未加备份脚本。
- **spec §4.1 C5 的修正**（见 Task 6 的说明）与**已知局限**（同一文件走 wiki 与 RAG 两条路径
  会产生两行 catalog、两个不同的 `content_hash`，P1 的唯一索引不会判其重复）。
- **备份门禁缺口**：PG 备份 cron 已确认静默失效（launchd 契约断裂，根因是 `/etc/crontab` 缺失），
  用户 2026-09-12 决定维持手动 —— 需要备份直接跑 `./scripts/backup_pg.sh`。此缺口不得粉饰。

- [ ] **Step 6: 提交**

```bash
git add backend/scripts/wiki_provenance_realdata.py Harness/changes/feat-wiki-provenance/
git commit -m "chore(wiki): P0 真实数据验证脚本 + 变更记录"
```

---

## 验收对照（spec §十）

| 验收信号 | 由哪个任务交付 |
|---|---|
| Milvus chunk 带正确 `page_number` / `section_name` / `paragraph_no` | Task 2、Task 3 |
| `document_catalog` 有**预留** `storage_url` + 非空 `content_hash` | Task 5、Task 6 |
| 预留键确定性（同输入同 URL），形状为 `s3://qa-knowledge-sources/sources/<h[:2]>/<h>/<name>` | Task 4 Step 1、Task 7 Step 2 |

> **原验收信号「MinIO 桶内存有源文件」已删除** —— 用户 2026-09-12 决定源文件字节暂不存储。
> 验收时**不得**把「桶里有文件」当作通过条件；该项由「预留键确定性」替代。

## 不做的事（范围边界）

- **不存储源文件字节**（用户 2026-09-12 决定）。只预留位置，`storage_url` **当前不可读**。
- **不引入 `minio` 依赖 / 不加容器 / 不加备份脚本** —— 存储能力真正启用时再说。
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

### 2. 摘要口径必须一致 —— 收敛为**一份实现**

| 计划 | 函数 | 定义 |
|---|---|---|
| P0（Task 4） | `source_locator.hashBytes(content: bytes) -> str` | `sha256(content).hexdigest()` |
| P0（Task 4） | `source_locator.hashText(text: str) -> str` | `hashBytes(text.encode("utf-8"))` |
| P1（Task 1） | `wiki_page_service.contentHashOf(text: str) -> str` | `sha256(text.encode("utf-8")).hexdigest()` |

三者对同一文本产出**同一个值**，P0 的 Task 6 依赖这一点写 `document_catalog.content_hash`。

**收敛动作（先落地的一方负责）**：P0 落地后，把 P1 的 `contentHashOf` 改为
`from app.infrastructure.source_locator import hashText` 的薄包装（或直接替换调用点）。
**不要保留两份 `sha256` 实现** —— 它们现在是同一算法，但会各自漂移。
P0 的 Task 6 不依赖 P1（`hashText` 已满足口径），所以两边可以任意顺序落地。

无论哪种，两边测试都要**把 64 位摘要钉成字面量**（而不是只断言长度）。
只断言长度挡不住口径漂移 —— 漂移了不会报错，只会让两侧比对永远不等。
P0 已在 `test_rag_service.py` 钉死 `sha256(b"dummy")` 的字面量。

### 3. `wiki_import_service.execute` 是 P0 与 P1 的共同改动点

| 计划 | 改 `execute` 的什么 |
|---|---|
| P0 Task 6 | 在**末尾**（任务台账落库之后）追加 `document_catalog` 登记 |
| P1 Task 4 | 重写循环体与 `_importOne`，把重复项从「失败」改判为「跳过」，台账加 `skipped_pages` |

两者不冲突，但 P0 的插入位置写的是「任务台账落库之后」这种**位置锚点**，
而 P1 恰好会改那段台账代码。**后执行的一方必须重新定位锚点**，不要照着行号改。

另一个交互：P1 的导入测试用 `autoClassify: false`（不调 LLM）；P0 的 Task 6 测试传
`modelId` 并 patch `_INVOKER_CLIENT`。两者各自独立成立，互不影响。

### 4. `TextBlock` 只属于 P0

P1 与 P3 都**不依赖** `TextBlock` / `Chunk.metadata` / `source_locator`（P1 计划已显式声明解耦）。
P3 的 `evidence` 写入若要填 `page_number` / `section_name` / `paragraph_no`，来源正是 P0 落进
Milvus 的这三列 —— 那是**数据上的**依赖，不是代码依赖，故 P3 不阻塞于 P0，但 P0 未落地时
P3 写出的 evidence 定位符只能是空值。这一点在 P3 计划里核对。
