"""催货单导出服务（Excel 明细 + Word 催货函）。

范式沿用 ``evaluation_report_export_service``：**只接收 immutable payload，
不接触 DB/ORM/FastAPI**；由 API 层从 ORM 拼好 payload 再调用。

设计要点：
- 表头/抬头里的公司法定名称来自 ``system_config`` 键
  ``chase.letter.company_name``（由 API 层读取后传入）；**未配置 ⇒ 整行不渲染**，
  不写死任何默认名（写死一个「看起来像公司名」的值比留空更危险）。
- 供应商名由 API 层经 ``loadMdmDirectory`` 查得；查不到 ⇒ 回落编码。
- 中文字体：docx 侧用 ``w:eastAsia`` 设置（``pdf_fonts.ensureCjkFont`` 是 PDF 专用）。
- 审批留痕（``decisions``）**不渲染**进任何产物：催货函与明细都是**外发给供应商**
  的材料，内部审批轨迹不应外泄；该字段仅随 payload 传递，供 API 层自行取用。
- **对外材料三道安全/正确性护栏**（2026-11-28 终审）：
  1. **Excel 公式注入**：外部数据以 ``=`` 开头 ⇒ openpyxl 会置 ``data_type='f'``
     ⇒ 变成活公式 ⇒ 写入后强制 ``data_type='s'``（见 ``_writeExcelCell``）；
  2. **时间口径**：对外文件一律按业务时区（UTC+8）渲染并标注时区（见
     ``_BUSINESS_TZ``）——否则对外承诺期限会比系统口径早 8 小时；
  3. **合计失实陈述**：数量未知（``open_qty`` 为 NULL）时**不得**写「合计欠交 0」
     （那等于向供应商断言没有欠交）⇒ 见 ``_totalSummary``。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

#: Excel 抬头信息 Sheet 名
_INFO_SHEET_NAME = "催货单信息"
#: Excel 明细 Sheet 名（表头在第 1 行）
_DETAIL_SHEET_NAME = "催货明细"

#: 公司法定名称的系统配置键（**唯一事实源**：API 层读它、种子脚本播种它）。
#:
#: 未配置 ⇒ 催货函抬头整行不渲染（**不写死任何默认公司名**）。该行必须**存在**
#: 于 `system_config`（种子脚本播种，value 为 NULL）⇒ 否则 admin 在后台
#: `PUT /admin/system-config/{key}` 会拿到 404，**根本改不了抬头**。
COMPANY_NAME_CONFIG_KEY = "chase.letter.company_name"

#: docx 正文中文字体（宋体；仅设置 w:eastAsia/ascii 名称，不内嵌字体）
_CJK_FONT = "SimSun"

#: 对外文件的**业务时区**（固定 +08:00）。
#:
#: ⚠️ **不要用 ``ZoneInfo("Asia/Shanghai")``** —— 容器（python:3.12-slim）可能没装
#: tzdata ⇒ ``ZoneInfoNotFoundError``。中国无夏令时，固定 +08:00 完全正确。
#: 库里的 ``timestamptz`` 经 asyncpg 返回 **UTC aware** ⇒ 直接渲染会把
#: 「2026-12-01 23:59」写成对外承诺 ⇒ 比系统口径（页面 ``toLocaleString()``）
#: **早 8 小时** ⇒ 必须先转业务时区。
_BUSINESS_TZ = timezone(timedelta(hours=8))

#: 对外文件里的**时区标注**（每个时刻都带它 ⇒ 供应商不会把墙钟当本地时间）。
_TZ_LABEL = "（北京时间 UTC+8）"

#: Excel 明细列（顺序即表头顺序）
_EXCEL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("订单号", "poNo"),
    ("行号", "poLineNo"),
    ("物料编码", "materialCode"),
    ("物料描述", "materialDesc"),
    ("订购量", "orderedQty"),
    ("已收量", "receivedQty"),
    ("欠交量", "openQty"),
    ("期望到货日", "expectedDate"),
    ("逾期天数", "overdueDays"),
    ("脏数据标记", "isDirty"),
)

#: Word 明细表列（顺序即表头顺序）
_DOCX_COLUMNS: tuple[tuple[str, str], ...] = (
    ("订单号", "poNo"),
    ("行号", "poLineNo"),
    ("物料描述", "materialDesc"),
    ("欠交量", "openQty"),
    ("期望到货日", "expectedDate"),
    ("逾期天数", "overdueDays"),
)

_HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")


@dataclass(frozen=True)
class ChaseExportPayload:
    """导出输入 payload（不可变 dataclass）。"""

    chaseDraftId: str
    status: str
    supplierCode: str
    supplierName: str | None
    companyName: str | None
    expectedReplyDeadline: datetime | None
    notes: str | None
    createdBy: str
    createdTime: datetime
    lines: list[dict[str, Any]]
    decisions: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# 取值辅助（payload 里的数量字段可能是 Decimal / int / None）
# ---------------------------------------------------------------------------


def _toDecimal(value: Any) -> Decimal:
    """把任意数量值转 Decimal；无法解析时回落 0（不抛异常，导出不应因脏值中断）。"""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _formatDecimalNumber(quantity: Decimal) -> str:
    """数值格式化：去掉多余尾零，且不出现科学计数法（``Decimal('6E+1')`` ⇒ ``60``）。"""
    normalized = quantity.normalize()
    if normalized == normalized.to_integral_value():
        return str(int(normalized))
    return f"{normalized:f}"


def _formatQuantity(value: Any) -> str:
    """数量单元格文本：``None``（**数量未知**）⇒ 空串；``0`` 是合法值 ⇒ 显示 ``0``。

    数量未知与数量为零**语义不同**：把 NULL 呈现成 ``0`` 等于凭空断言「已收满」，
    因此渲染层留空；合计口径仍按 0 计（未知量不贡献欠交量）。
    """
    if value is None:
        return ""
    return _formatDecimalNumber(_toDecimal(value))


def _text(value: Any) -> str:
    """单元格文本：``None`` ⇒ 空串；``0``/``False`` 保留（不当作缺省值抹掉）。"""
    return "" if value is None else str(value)


def _toBusinessTz(value: datetime) -> datetime:
    """把 datetime 转到**业务时区**（UTC+8）。

    **naive 视为 UTC**：库里 ``timestamptz`` 经 asyncpg 返回 aware UTC；naive 值
    一律按 UTC 解读后再转 +08:00（避免「按容器本地时区解读」这种隐式依赖）。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_BUSINESS_TZ)


def _formatDate(value: Any) -> str:
    """日期时间格式化（**业务时区**）：

    - ``None`` ⇒ 空串；
    - ``datetime`` ⇒ 转业务时区后的 ``YYYY-MM-DD``（带时区标注）；
    - ``date``（纯日期，无时区概念）⇒ ``YYYY-MM-DD``，**不带**标注；
    - 其余 ⇒ 原样字符串化。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return f"{_toBusinessTz(value).strftime('%Y-%m-%d')}{_TZ_LABEL}"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _formatDeadline(value: datetime | None) -> str:
    """回复期限（**业务时区**）：带时刻用 ``YYYY-MM-DD HH:MM``，只带日期用 ``YYYY-MM-DD``。

    两种形态都带时区标注 —— 对外文件里的期限是**承诺**，供应商必须知道时区。
    """
    if value is None:
        return ""
    local = _toBusinessTz(value)
    if local.hour or local.minute:
        return f"{local.strftime('%Y-%m-%d %H:%M')}{_TZ_LABEL}"
    return f"{local.strftime('%Y-%m-%d')}{_TZ_LABEL}"


def _formatSupplier(supplierCode: str, supplierName: str | None) -> str:
    """供应商抬头（Excel 与 Word **同一口径**）。

    - 有名字 ⇒ ``名字（编码）``；
    - 无名字（MDM 查不到）⇒ **只显示编码**。

    ⚠️ 不能写成 ``名字 or 编码（编码）`` —— 那会在无名字时产出 ``F109（F109）``
    这种冗余抬头（Excel 与 Word 还会各写各的）。
    """
    if supplierName:
        return f"{supplierName}（{supplierCode}）"
    return supplierCode


# ---------------------------------------------------------------------------
# Payload 拼装
# ---------------------------------------------------------------------------


def buildChaseExportPayload(
    *,
    draft: Any,
    lines: list[Any],
    decisions: list[Any],
    supplierName: str | None,
    companyName: str | None,
) -> ChaseExportPayload:
    """从 ORM 行拼出导出 payload（纯拼装，不查库）。

    ``draft``/``lines``/``decisions`` 只按属性读取，**不 import ORM 类型**，
    因此 API 层可以传 ORM 实例，也可以传任意同形对象（测试友好）。

    ORM 列（``expected_reply_deadline`` / ``notes``）**直接取属性**、不用
    ``getattr(..., None)`` 兜底：列确实存在，写错属性名应当 fail-fast，
    而不是静默产出空值（仓内「守卫空集假通过」同款陷阱）。
    """
    return ChaseExportPayload(
        chaseDraftId=str(draft.chase_draft_id),
        status=str(draft.status),
        supplierCode=str(draft.supplier_code),
        supplierName=supplierName,
        companyName=companyName,
        expectedReplyDeadline=draft.expected_reply_deadline,
        notes=draft.notes,
        createdBy=str(draft.created_by),
        createdTime=draft.created_time,
        lines=[
            {
                "poNo": line.po_no,
                "poLineNo": line.po_line_no,
                "materialCode": line.material_code,
                "materialDesc": line.material_desc,
                "orderedQty": line.ordered_qty,
                "receivedQty": line.received_qty,
                "openQty": line.open_qty,
                "expectedDate": line.expected_date,
                "overdueDays": line.overdue_days,
                "isDirty": line.is_dirty,
            }
            for line in lines
        ],
        decisions=[
            {
                "decision": decision.decision,
                "actor": decision.actor,
                "note": decision.note,
                "decidedTime": decision.decided_time,
            }
            for decision in decisions
        ],
    )


# ---------------------------------------------------------------------------
# Excel 渲染
# ---------------------------------------------------------------------------


def _buildInfoRows(payload: ChaseExportPayload) -> list[tuple[str, str]]:
    """抬头信息区（标签, 取值）。"""
    return [
        ("催货单号", payload.chaseDraftId),
        ("供应商", _formatSupplier(payload.supplierCode, payload.supplierName)),
        ("回复期限", _formatDeadline(payload.expectedReplyDeadline)),
        ("状态", payload.status),
        ("创建人", payload.createdBy),
        ("创建时间", _formatDate(payload.createdTime)),
        ("备注", payload.notes or ""),
    ]


def _writeExcelCell(sheet: Any, row: int, column: int, value: Any) -> Any:
    """写单元格并**强制字符串按文本存储**（防 Excel 公式注入）。

    openpyxl 在**写入时**判定公式：``value`` 以 ``=`` 开头且未显式给 data_type
    ⇒ 置 ``'f'`` ⇒ 单元格变成**活公式**（``=HYPERLINK(...)`` 外链、
    ``=cmd|...`` 命令），供应商在 Excel 里打开即执行 ⇒ 对外材料的安全事故。

    ⇒ 必须在 ``cell.value = ...`` **之后**再覆盖 ``data_type='s'``（内容原样，
    Excel 按文本存储）。**非字符串**（数字/日期/布尔）**不动** data_type。
    """
    cell = sheet.cell(row=row, column=column, value=value)
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def renderChaseExcel(payload: ChaseExportPayload) -> bytes:
    """Excel：两个 Sheet —— 「催货单信息」（抬头）+ 「催货明细」（表头在第 1 行）。

    明细 Sheet 的表头固定在第 1 行、数据从第 2 行起 ⇒ 表头行号与数据行数
    一一对应，程序化读取（openpyxl）时不必再猜表头在哪一行。

    所有外部数据（物料描述 / 供应商名 / 备注 / 单号）都经 ``_writeExcelCell``
    ⇒ **强制文本存储**，不会被当成活公式。
    """
    book = Workbook()
    book.remove(book.active)

    infoSheet = book.create_sheet(_INFO_SHEET_NAME)
    for rowIndex, (label, value) in enumerate(_buildInfoRows(payload), start=1):
        labelCell = _writeExcelCell(infoSheet, rowIndex, 1, label)
        labelCell.font = Font(bold=True)
        _writeExcelCell(infoSheet, rowIndex, 2, value)
    infoSheet.column_dimensions["A"].width = 16
    infoSheet.column_dimensions["B"].width = 48

    detailSheet = book.create_sheet(_DETAIL_SHEET_NAME)
    for colIndex, (title, _key) in enumerate(_EXCEL_COLUMNS, start=1):
        cell = _writeExcelCell(detailSheet, 1, colIndex, title)
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
    for offset, line in enumerate(payload.lines, start=1):
        for colIndex, (_title, key) in enumerate(_EXCEL_COLUMNS, start=1):
            _writeExcelCell(detailSheet, 1 + offset, colIndex, line.get(key))
    for colIndex in range(1, len(_EXCEL_COLUMNS) + 1):
        detailSheet.column_dimensions[get_column_letter(colIndex)].width = 16
    detailSheet.freeze_panes = detailSheet.cell(row=2, column=1)

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Word 渲染
# ---------------------------------------------------------------------------


def _setCjkFont(document: Any, size: int = 10) -> None:
    """给 docx 的 Normal 样式设中文字体（``w:eastAsia``）。

    ``rPr``/``rFonts`` 用 ``get_or_add_*`` 显式取，**不依赖** 上一行
    ``style.font.name =`` 的副作用（否则换个调用顺序就 ``AttributeError``）。
    """
    style = document.styles["Normal"]
    style.font.name = _CJK_FONT
    style.font.size = Pt(size)
    rFonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    rFonts.set(qn("w:eastAsia"), _CJK_FONT)


def _writeCell(cell: Any, text: str, *, bold: bool = False) -> None:
    """写单元格文本（保留首行 run，便于加粗）。"""
    cell.text = text
    if bold:
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True


def _totalOpenQuantity(lines: list[dict[str, Any]]) -> Decimal:
    """合计欠交量（Decimal 累加，避免浮点误差）。

    ``openQty`` 为 NULL（数量未知）时按 0 计入合计 —— 未知量不贡献欠交量，
    但**显示层仍留空**（见 ``_formatQuantity``），两者语义不同。
    """
    return sum((_toDecimal(line.get("openQty")) for line in lines), Decimal("0"))


def _countUnknownQuantity(lines: list[dict[str, Any]]) -> int:
    """数量未知（``openQty`` 为 NULL）的行数。"""
    return sum(1 for line in lines if line.get("openQty") is None)


def _totalSummary(lines: list[dict[str, Any]]) -> str:
    """合计欠交文案（**对外事实陈述** ⇒ 未知量必须显式说明）。

    三种形态：

    - 全部已知 ⇒ 「合计欠交 X。」
    - 部分未知 ⇒ 「合计欠交 X（其中 M 行数量待确认，未计入）。」
    - **全部未知** ⇒ 「欠交数量待确认（全部 N 行数量未知，未计入合计）。」

    ⚠️ 全部未知时**绝不能**写「合计欠交 0」—— 那等于向供应商断言「没有欠交」，
    是**失实陈述**（明细单元格层面已正确区分「未知」与「0」，合计层必须跟上）。
    """
    unknownCount = _countUnknownQuantity(lines)
    totalOpen = _totalOpenQuantity(lines)
    if unknownCount == 0:
        return f"合计欠交 {_formatDecimalNumber(totalOpen)}。"
    if unknownCount == len(lines):
        return f"欠交数量待确认（全部 {unknownCount} 行数量未知，未计入合计）。"
    return (
        f"合计欠交 {_formatDecimalNumber(totalOpen)}"
        f"（其中 {unknownCount} 行数量待确认，未计入）。"
    )


def renderChaseDocx(payload: ChaseExportPayload) -> bytes:
    """Word：抬头 → 催货说明 → 明细表 → 落款。

    公司法定名称未配置 ⇒ **整行不渲染**（不留空白行、不拿品牌名冒充）。
    """
    document = Document()
    _setCjkFont(document)

    if payload.companyName:
        document.add_paragraph(payload.companyName)
    document.add_paragraph(f"催货单号：{payload.chaseDraftId}")
    document.add_paragraph(
        f"供应商：{_formatSupplier(payload.supplierCode, payload.supplierName)}"
    )
    if payload.expectedReplyDeadline:
        document.add_paragraph(f"回复期限：{_formatDeadline(payload.expectedReplyDeadline)}")

    explain = (
        f"催货说明：本次催货单共 {len(payload.lines)} 行订单明细，"
        f"{_totalSummary(payload.lines)}"
    )
    if payload.expectedReplyDeadline:
        explain += f"请于 {_formatDeadline(payload.expectedReplyDeadline)} 前回复处理计划。"
    document.add_paragraph(explain)

    table = document.add_table(rows=1, cols=len(_DOCX_COLUMNS))
    table.style = "Table Grid"
    for index, (title, _key) in enumerate(_DOCX_COLUMNS):
        _writeCell(table.rows[0].cells[index], title, bold=True)
    for line in payload.lines:
        cells = table.add_row().cells
        for index, (_title, key) in enumerate(_DOCX_COLUMNS):
            value = line.get(key)
            if key == "openQty":
                text = _formatQuantity(value)
            elif key == "expectedDate":
                text = _formatDate(value)
            elif key == "materialDesc":
                text = _text(value) or _text(line.get("materialCode"))
            else:
                text = _text(value)
            _writeCell(cells[index], text)

    if payload.notes:
        document.add_paragraph(f"备注：{payload.notes}")
    document.add_paragraph(f"催货人：{payload.createdBy}")
    document.add_paragraph(f"日期：{_formatDate(payload.createdTime)}")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
