"""文档解析器：把 PDF/DOCX/MD 解析成带定位信息的文本块。

P0 溯源地基：原实现返回扁平字符串，PDF 的页码与 DOCX 的段落结构在
``"\\n".join(...)`` 处被丢弃，导致下游证据链无出处可填。本实现改为返回
``list[TextBlock]``，把定位符一路带到 Chunk 与 Milvus。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


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

    # PPTX（OOXML 包），**.ppt** 是老式二进制，python-pptx 读不了 —— 显式
    # 抛 UnsupportedFileTypeError 让前端告诉用户「请另存为 .pptx」而不是
    # 误导性的「文件损坏」。
    if (
        mime_type
        == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        or ext == "pptx"
    ):
        return _parsePptx(content)
    if ext == "ppt":
        raise UnsupportedFileTypeError(
            f"Legacy .ppt (binary) is not supported. Please re-save as .pptx: {filename}"
        )

    # Excel 双格式分叉：
    # - xlsx（OOXML 包）→ openpyxl
    # - xls  （OLE2 二进制）→ xlrd 2.x（**只读 .xls**，2.x 已不再支持 xlsx）
    if (
        mime_type
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        or ext == "xlsx"
    ):
        return _parseXlsx(content)
    if ext == "xls":
        return _parseXls(content)

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
    """解析 PDF：每页按空行切段，段号在页内重新计数。

    策略：
    1. pypdf — 优先（原生文本 PDF 最快）
    2. pdfplumber — pypdf 文本过少时的 fallback（含表格时结构更准）
    3. pdftoppm + pytesseract OCR — 扫描件/图片型 PDF 的终极 fallback

    判定逻辑修订（2026-09-14）：
    原版用 ``total_chars < 20`` 判定扫描件，对纯中文短文本（一句标题就
    只 6 字符）误判。修正：用「前两步是否成功抽出**非空内容**」判定，
    0 字符 = 真的是扫描件或损坏文件；只要有任何字符就放行，OCR 仅作
    增强。
    """
    blocks: list[TextBlock] = []
    total_chars = 0

    # Step 1: pypdf（最快的文字型 PDF 提取，无表格结构）
    try:
        blocks = _parsePdfWithPypdf(content)
        total_chars = sum(len(b.text) for b in blocks)
    except Exception:
        pass

    # Step 2: pdfplumber（保留表格结构 + 部分 PDF 文字更准）。
    # 仅在 pypdf 抽出 0 字符时启用 —— 否则会重复输出同一段文字。
    # 表格结构带来的精度收益不足以覆盖重复文本带来的脏数据。
    if total_chars == 0:
        try:
            pdfplumber_blocks = _parsePdfWithPdfplumber(content)
            pdfplumber_chars = sum(len(b.text) for b in pdfplumber_blocks)
            if pdfplumber_chars > total_chars:
                blocks = pdfplumber_blocks
                total_chars = pdfplumber_chars
        except Exception:
            pass

    # Step 3: OCR fallback（扫描件/图片型 PDF）
    if total_chars == 0:
        try:
            ocr_blocks = _parsePdfWithOcr(content)
            ocr_chars = sum(len(b.text) for b in ocr_blocks)
            if ocr_chars > total_chars:
                blocks = ocr_blocks
                total_chars = ocr_chars
        except DocumentParserError:
            # OCR 依赖未装 / pdftoppm 不可用 —— 静默跳过，让上层按"扫描件"
            # 以外的路径处理（缺 tesseract 时不是扫描件，是环境配置问题）
            pass
        except Exception:
            # OCR 跑挂（pdftoppm 失败 / 单页超时）也静默降级 —— 文字型 PDF
            # 早已被前两步覆盖，OCR 失败不影响主路径。
            pass

    if total_chars == 0:
        raise DocumentParserError(
            "PDF 解析完成但未提取到任何文本，可能为扫描件或图片型 PDF"
        )
    return blocks


def _parsePdfWithPypdf(content: bytes) -> list[TextBlock]:
    """用 pypdf 解析 PDF。"""
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


def _parsePdfWithPdfplumber(content: bytes) -> list[TextBlock]:
    """用 pdfplumber 解析 PDF（pypdf fallback，含表格时结构更准）。

    表格提取策略：先调 ``page.extract_tables()``，把表格渲为 markdown table
    文本（保留列对齐），再与普通段落文本合并。空表/无表 fallback 到
    ``extract_text``，不丢内容。
    """
    import io
    import pdfplumber
    blocks: list[TextBlock] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for pageIdx, page in enumerate(pdf.pages):
            paraNo = 0
            # 表格 → markdown 形式，列对齐保留为 ``|`` 分隔
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for table in tables:
                rendered = _renderTableAsMarkdown(table)
                if not rendered:
                    continue
                paraNo += 1
                blocks.append(
                    TextBlock(
                        text=rendered,
                        page_number=pageIdx + 1,
                        section_name=None,
                        paragraph_no=paraNo,
                    )
                )
            # 文本段落
            pageText = (page.extract_text() or "").strip()
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


def _renderTableAsMarkdown(table: list[list[str | None]]) -> str:
    """把 pdfplumber 的 ``list[list[Optional[str]]]`` 渲成 markdown table。

    pdfplumber 把空单元格留为 ``None``：本实现渲为空字符串，不丢列对齐。
    表格至少要有 1 行 1 列才渲 —— 全空表格跳过。
    """
    if not table:
        return ""
    cleaned: list[list[str]] = []
    for row in table:
        cells = [(c or "").strip().replace("\n", " ") for c in row]
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return ""
    # 列数取最大行长（短行右补空），保证 markdown 对齐
    width = max(len(r) for r in cleaned)
    for r in cleaned:
        r.extend([""] * (width - len(r)))
    lines = ["| " + " | ".join(cleaned[0]) + " |"]
    lines.append("|" + "|".join(["---"] * width) + "|")
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _parsePdfWithOcr(content: bytes) -> list[TextBlock]:
    """用 pdftoppm + pytesseract 对 PDF 页面做 OCR（处理扫描件/图片型 PDF）。

    依赖：poppler-utils（pdftoppm）、tesseract-ocr、tesseract-ocr-chi-sim。
    """
    import io
    import os
    import shutil
    import subprocess
    import tempfile

    # Render PDF pages to JPEG images using pdftoppm
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "doc.pdf")
        img_prefix = os.path.join(tmpdir, "page")
        with open(pdf_path, "wb") as f:
            f.write(content)
        # -r 150: 150 DPI; -jpeg: output JPEG; -f 1 -l N: first to last page
        result = subprocess.run(
            ["pdftoppm", "-r", "150", "-jpeg", "-f", "1", pdf_path, img_prefix],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise DocumentParserError(f"pdftoppm failed: {result.stderr}")

        # Collect rendered pages
        pages_dir = os.path.join(tmpdir, "pages")
        os.makedirs(pages_dir, exist_ok=True)
        for fname in sorted(os.listdir(tmpdir)):
            if fname.startswith("page-") and fname.endswith(".jpg"):
                shutil.move(os.path.join(tmpdir, fname), pages_dir)

        page_files = sorted(os.listdir(pages_dir))
        if not page_files:
            raise DocumentParserError("pdftoppm produced no page images")

        # OCR each page with pytesseract
        import pytesseract
        blocks: list[TextBlock] = []
        for pageIdx, fname in enumerate(page_files):
            img_path = os.path.join(pages_dir, fname)
            text = pytesseract.image_to_string(img_path, lang="chi_sim+eng", timeout=60)
            pageText = text.strip()
            if not pageText:
                continue
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


# 单 sheet 渲出的文本上限。Excel 大表按 sheet 拆块，每块仍受上游
# ``_MAX_SOURCE_CHARS``（1MB）兜底 —— 这里把每 sheet 自身先按行数封顶，
# 避免单 sheet 10000 行就把整文件吃光预算。
_MAX_EXCEL_ROWS_PER_SHEET = 2000
_MAX_EXCEL_CELL_CHARS = 200


def _parsePptx(content: bytes) -> list[TextBlock]:
    """解析 PPTX：每张幻灯片 = 一个 TextBlock。

    定位语义：
    - ``section_name`` = 第一个文本框（通常作标题）
    - ``page_number`` = None（PPT 没有页概念，区别于 PDF）
    - ``paragraph_no`` = 幻灯片序号，文档内递增

    关键决策（2026-09-14）：
    旧实现没 PPT 解析，``Phase 5`` 引入。每张幻灯片的所有文本框按添加
    顺序合并为一个段落（不去重、不分段）—— PPT 的"段"概念弱，强行按
    框切会让一份 30 页的培训手册变 300 块，反切分时只能按空行切，毫无
    价值。合并后由 Markdown 切分器按 ``#`` 重新识别。
    """
    try:
        import io

        from pptx import Presentation

        prs = Presentation(io.BytesIO(content))
    except Exception as e:
        raise DocumentParserError(f"PPTX parsing failed: {e}") from e

    blocks: list[TextBlock] = []
    for idx, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        section: str | None = None
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            text = shape.text_frame.text.strip()
            if not text:
                continue
            if section is None:
                section = text  # 第一个文本框当 section 标题
            parts.append(text)
        if not parts:
            continue
        blocks.append(
            TextBlock(
                text="\n".join(parts),
                page_number=None,
                section_name=section,
                paragraph_no=len(blocks) + 1,
            )
        )
    return blocks


def _parseXlsx(content: bytes) -> list[TextBlock]:
    """解析 XLSX：每个 sheet = 一个 TextBlock（结构化表格文本）。

    表格渲为 markdown table：首行表头 + 后续行数据，列间 ``|`` 对齐。
    ``page_number`` = None；``section_name`` = sheet 名。

    大表保护：超过 ``_MAX_EXCEL_ROWS_PER_SHEET`` 行的 sheet 只取前 N 行
    并在末尾追加截断标记 —— 让下游切分器能看到「这是张表」而不是被静
    默截断。

    兜底策略（2026-09-14）：openpyxl 对某些第三方导出（WPS / 某些 BI
    工具）的 xlsx 抛 ``could not read stylesheet from None``，但实际数
    据完整。openpyxl 失败时降级到 ``_parseXlsxRawFallback`` 直接读 OOXML
    —— 只取 cell 值，不碰 stylesheet，能解开绝大多数此类文件。
    """
    try:
        import io

        from openpyxl import load_workbook

        # ``data_only=True``：对有公式的 cell 取**缓存的计算结果**而非公式
        # 字符串，否则下游拿到的是 ``=SUM(B1:B5)`` 而不是数字。
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as primaryError:
        logger.info(
            "openpyxl 解析 xlsx 失败，降级到 raw XML 读取: %s", primaryError
        )
        try:
            return _parseXlsxRawFallback(content)
        except Exception as fallbackError:
            # 兜底也挂才当作真错误上抛 —— 此时文件多半真损坏
            raise DocumentParserError(
                f"XLSX parsing failed: openpyxl={primaryError}; raw={fallbackError}"
            ) from fallbackError

    blocks: list[TextBlock] = []
    try:
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            rendered = _renderSheetAsMarkdown(
                ws.title, rows, truncated=False
            )
            if rendered is None:
                continue
            blocks.append(
                TextBlock(
                    text=rendered,
                    page_number=None,
                    section_name=ws.title,
                    paragraph_no=len(blocks) + 1,
                )
            )
    finally:
        wb.close()
    return blocks


def _parseXls(content: bytes) -> list[TextBlock]:
    """解析老式 .xls（OLE2 二进制）—— 走 xlrd 2.x。

    xlrd 2.x 已**只读 .xls**：读 .xlsx 会抛 ``XLRDError``。dispatch 已
    把 .xlsx 路由到 ``_parseXlsx``，这里只处理 .xls。

    已知 xlrd 2.x 限制（用户常踩）：
    - BIFF4（Excel 95 早期）格式不支持
    - 含 chart-only / VBA project 的工作簿可能解析失败
    - .xls 后缀但实际是 .xlsx 改名（Excel 偶发）→ 抛 ``XLRDError: Excel
      xlsx file; not supported``
    这三种情况给前端一句具体提示（"用 WPS/Excel 另存为 xlsx"），而不是
    笼统的「文件损坏」。
    """
    try:
        import io

        import xlrd

        wb = xlrd.open_workbook(file_contents=content)
    except xlrd.XLRDError as e:
        msg = str(e)
        if "xlsx file" in msg:
            raise DocumentParserError(
                "文件扩展名是 .xls 但实际是 Excel 2007+ 的 xlsx 格式。"
                "请在 Excel/WPS 中『另存为 → Excel 97-2003 工作簿 (.xls)』"
                "或直接改后缀为 .xlsx 重试。"
            ) from e
        if "BIFF" in msg or "unsupported" in msg.lower():
            raise DocumentParserError(
                f"无法解析此 .xls 文件（xlrd 不支持 BIFF4/BIFF5 等极早期格式）：{msg}"
            ) from e
        raise DocumentParserError(f"XLS 解析失败：{msg}") from e
    except Exception as e:
        raise DocumentParserError(f"XLS parsing failed: {e}") from e

    blocks: list[TextBlock] = []
    for sheet in wb.sheets():
        rows: list[list[str]] = []
        for r in range(sheet.nrows):
            row: list[str] = []
            for c in range(sheet.ncols):
                cell = sheet.cell_value(r, c)
                row.append("" if cell is None else str(cell))
            rows.append(row)
        rendered = _renderSheetAsMarkdown(sheet.name, rows, truncated=False)
        if rendered is None:
            continue
        blocks.append(
            TextBlock(
                text=rendered,
                page_number=None,
                section_name=sheet.name,
                paragraph_no=len(blocks) + 1,
            )
        )
    return blocks


def _parseXlsxRawFallback(content: bytes) -> list[TextBlock]:
    """直接读 OOXML 包兜底 openpyxl 的 stylesheet 解析 bug。

    已知触发场景（实测）：WPS 导出、某些 BI 工具导出、以及被 Excel
    "修复"过的 xlsx —— styles.xml 合法但 openpyxl 内部样式解析器对
    其内容校验失败，抛 ``could not read stylesheet from None``。

    本路径只取 cell 值（绕过样式系统），保证数据完整提取。公式 cell
    取公式字符串而非计算结果 —— openpyxl 路径拿的是 ``data_only=True``
    缓存值，本路径拿不到。
    """
    import io
    import zipfile
    from xml.etree import ElementTree as ET

    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

    zf = zipfile.ZipFile(io.BytesIO(content))

    # 1. 解析 workbook.xml：sheet 顺序 + sheetId → sheet 文件路径
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    sheetsOrder: list[tuple[str, str]] = []  # (sheet_name, sheet_path)
    sheetByRid: dict[str, str] = {}
    for sh in workbook.iter(f"{NS}sheet"):
        rid = sh.get(f"{REL_NS}id") or ""
        name = sh.get("name") or ""
        sheetByRid[rid] = name
    wbRels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in wbRels.iter(
        "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
    ):
        rid = rel.get("Id")
        target = rel.get("Target") or ""
        if rid in sheetByRid:
            # target 是相对 xl/ 的路径
            path = "xl/" + target if not target.startswith("/") else target.lstrip("/")
            sheetsOrder.append((sheetByRid[rid], path))

    # 2. 解析 sharedStrings.xml（cell type="s" 的索引）
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        ssRoot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in ssRoot.iter(f"{NS}si"):
            text = "".join((t.text or "") for t in si.iter(f"{NS}t"))
            shared.append(text)

    # 3. 逐 sheet 抽 cell 值
    blocks: list[TextBlock] = []
    for sheetName, sheetPath in sheetsOrder:
        if sheetPath not in zf.namelist():
            continue
        rows: list[list[str]] = []
        try:
            sheetRoot = ET.fromstring(zf.read(sheetPath))
        except ET.ParseError:
            continue
        for row in sheetRoot.iter(f"{NS}row"):
            cells: list[str] = []
            for c in row.findall(f"{NS}c"):
                t = c.get("t")
                v = c.find(f"{NS}v")
                inline = c.find(f"{NS}is")
                if v is not None:
                    raw = v.text or ""
                    if t == "s":
                        try:
                            cells.append(shared[int(raw)])
                        except (ValueError, IndexError):
                            cells.append(raw)
                    else:
                        cells.append(raw)
                elif inline is not None:
                    # inlineStr：文本直接在 ``<is><t>...</t></is>``
                    cells.append("".join((tt.text or "") for tt in inline.iter(f"{NS}t")))
                else:
                    cells.append("")
            if any(cells):
                rows.append(cells)
        rendered = _renderSheetAsMarkdown(sheetName, rows, truncated=False)
        if rendered is None:
            continue
        blocks.append(
            TextBlock(
                text=rendered,
                page_number=None,
                section_name=sheetName,
                paragraph_no=len(blocks) + 1,
            )
        )
    return blocks


def _renderSheetAsMarkdown(
    sheetName: str,
    rows: list[list[object]],
    *,
    truncated: bool,
) -> str | None:
    """把二维行数据渲成 markdown table 字符串。

    返回 ``None`` 表示整张表都是空的（跳过该 sheet）；返回 ``""`` 表示
    表存在但全空单元格。
    """
    cleaned: list[list[str]] = []
    for row in rows:
        cells: list[str] = []
        for cell in row:
            if cell is None:
                cells.append("")
            else:
                s = str(cell).strip().replace("\n", " ")
                # 单 cell 长度封顶，防止「超大备注 cell」污染整块
                cells.append(s[:_MAX_EXCEL_CELL_CHARS])
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return None

    truncatedNote = ""
    if len(cleaned) > _MAX_EXCEL_ROWS_PER_SHEET:
        cleaned = cleaned[:_MAX_EXCEL_ROWS_PER_SHEET]
        truncatedNote = (
            f"\n\n_(已截断到前 {_MAX_EXCEL_ROWS_PER_SHEET} 行，原 sheet 共 "
            f"{len(cleaned) + 0} 行；如需全量请拆分文件)_"
        )

    width = max(len(r) for r in cleaned)
    for r in cleaned:
        r.extend([""] * (width - len(r)))

    lines = [f"## {sheetName}", ""]
    lines.append("| " + " | ".join(cleaned[0]) + " |")
    lines.append("|" + "|".join(["---"] * width) + "|")
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + truncatedNote
