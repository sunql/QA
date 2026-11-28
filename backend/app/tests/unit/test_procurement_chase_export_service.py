"""催货单导出服务：payload 拼装 + Excel/Word 渲染。"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from docx import Document
from openpyxl import load_workbook

from app.services.procurement_chase_export_service import (
    ChaseExportPayload,
    buildChaseExportPayload,
    renderChaseDocx,
    renderChaseExcel,
)


class _Draft:
    chase_draft_id = "d-1"
    status = "PENDING_APPROVAL"
    supplier_code = "F109"
    expected_reply_deadline = datetime(2026, 12, 1, tzinfo=UTC)
    notes = "请尽快回复"
    created_by = "admin"
    created_time = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)


class _Line:
    po_no = "PO1"
    po_line_no = "10"
    material_code = "M1"
    material_desc = "不锈钢板"
    ordered_qty = Decimal("100")
    received_qty = Decimal("40")
    open_qty = Decimal("60")
    expected_date = None
    overdue_days = 15
    is_dirty = False


class _Decision:
    decision = "SUBMIT"
    actor = "admin"
    note = "已核对"
    decided_time = datetime(2026, 11, 28, 10, 5, tzinfo=UTC)


def _line(**overrides: Any) -> dict[str, Any]:
    """一行明细（可覆写任意字段）。"""
    base: dict[str, Any] = {
        "poNo": "PO1",
        "poLineNo": "10",
        "materialCode": "M1",
        "materialDesc": "不锈钢板",
        "orderedQty": Decimal("100"),
        "receivedQty": Decimal("40"),
        "openQty": Decimal("60"),
        "expectedDate": None,
        "overdueDays": 15,
        "isDirty": False,
    }
    base.update(overrides)
    return base


def _payload(**overrides) -> ChaseExportPayload:
    base = dict(
        chaseDraftId="d-1",
        status="PENDING_APPROVAL",
        supplierCode="F109",
        supplierName="上海纳特",
        companyName="某某有限公司",
        expectedReplyDeadline=datetime(2026, 12, 1, tzinfo=UTC),
        notes="请尽快回复",
        createdBy="admin",
        createdTime=datetime(2026, 11, 28, 10, 0, tzinfo=UTC),
        lines=[_line()],
        decisions=[],
    )
    base.update(overrides)
    return ChaseExportPayload(**base)


def _docxText(blob: bytes) -> str:
    """从 docx（zip）里取 word/document.xml 文本。"""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def _docxTableRows(blob: bytes) -> list[list[str]]:
    """取 docx 第一张表的单元格文本（按行）。"""
    document = Document(io.BytesIO(blob))
    return [[cell.text for cell in row.cells] for row in document.tables[0].rows]


def test_build_payload_maps_orm_fields() -> None:
    payload = buildChaseExportPayload(
        draft=_Draft(),
        lines=[_Line()],
        decisions=[],
        supplierName="上海纳特",
        companyName="某某有限公司",
    )
    assert payload.chaseDraftId == "d-1"
    assert payload.supplierCode == "F109"
    assert payload.lines[0]["openQty"] == Decimal("60")
    assert payload.lines[0]["poLineNo"] == "10"


def test_build_payload_maps_decisions() -> None:
    payload = buildChaseExportPayload(
        draft=_Draft(),
        lines=[_Line()],
        decisions=[_Decision()],
        supplierName=None,
        companyName=None,
    )
    assert payload.decisions == [
        {
            "decision": "SUBMIT",
            "actor": "admin",
            "note": "已核对",
            "decidedTime": datetime(2026, 11, 28, 10, 5, tzinfo=UTC),
        }
    ]


def test_build_payload_fails_fast_on_missing_orm_column() -> None:
    """ORM 列写错属性名 ⇒ 直接抛错，不静默产出空值（守卫空集假通过的同款陷阱）。"""

    class _BrokenDraft:
        chase_draft_id = "d-2"
        status = "DRAFT"
        supplier_code = "F200"
        created_by = "buyer"
        created_time = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)

    with pytest.raises(AttributeError):
        buildChaseExportPayload(
            draft=_BrokenDraft(),
            lines=[],
            decisions=[],
            supplierName=None,
            companyName=None,
        )


def test_excel_contains_header_and_one_data_row() -> None:
    blob = renderChaseExcel(_payload())
    book = load_workbook(io.BytesIO(blob))
    sheet = book["催货明细"]
    headers = [c.value for c in sheet[1]]
    assert "订单号" in headers and "欠交量" in headers and "逾期天数" in headers
    assert sheet.max_row == 2, "1 行明细 ⇒ 2 行（含表头）"
    row = [c.value for c in sheet[2]]
    assert "PO1" in row


def test_excel_works_with_empty_lines() -> None:
    blob = renderChaseExcel(_payload(lines=[]))
    book = load_workbook(io.BytesIO(blob))
    assert book["催货明细"].max_row == 1, "无明细时只有表头"


def test_excel_info_sheet_carries_header_fields() -> None:
    blob = renderChaseExcel(_payload())
    book = load_workbook(io.BytesIO(blob))
    info = book["催货单信息"]
    values = [c.value for c in info["A"]]
    assert "催货单号" in values and "供应商" in values
    assert info["B2"].value == "上海纳特（F109）"


def test_docx_contains_key_texts() -> None:
    xml = _docxText(renderChaseDocx(_payload()))
    # docx 是 zip；用 zipfile 提取 document.xml 才能看到文本
    assert "上海纳特" in xml
    assert "某某有限公司" in xml
    assert "PO1" in xml
    # 断言具体文案而不是裸 "60"（裸数字几乎必然命中，定位不到问题）
    assert "本次催货单共 1 行订单明细，合计欠交 60。" in xml


def test_docx_omits_company_row_when_not_configured() -> None:
    """未配置公司名 ⇒ 整行不渲染（不留空白行、不拿品牌名冒充）。"""
    xml = _docxText(renderChaseDocx(_payload(companyName=None)))
    assert "上海纳特" in xml
    assert "某某有限公司" not in xml


def test_docx_omits_decision_trail() -> None:
    """审批留痕是内部轨迹 ⇒ 不外泄进给供应商的催货函。"""
    payload = _payload(
        decisions=[
            {
                "decision": "APPROVE",
                "actor": "internal-approver",
                "note": "内部意见",
                "decidedTime": datetime(2026, 11, 28, 10, 5, tzinfo=UTC),
            }
        ]
    )
    xml = _docxText(renderChaseDocx(payload))
    assert "internal-approver" not in xml
    assert "内部意见" not in xml


def test_quantity_zero_is_rendered_not_dropped() -> None:
    """欠交量 0 是合法值 ⇒ 不能被「空值兜底」抹成空串。"""
    payload = _payload(
        lines=[
            _line(
                poNo="PO2",
                poLineNo="20",
                materialCode="M2",
                materialDesc=None,
                orderedQty=Decimal("10"),
                receivedQty=Decimal("10"),
                openQty=Decimal("0"),
                overdueDays=0,
            )
        ]
    )
    rows = _docxTableRows(renderChaseDocx(payload))
    assert rows[1][3] == "0", "欠交量 0 是合法值，必须显示 0"
    xml = _docxText(renderChaseDocx(payload))
    assert "合计欠交 0" in xml


def test_unknown_open_qty_renders_blank_not_zero() -> None:
    """数量未知（ORM 允许 NULL）⇒ 显示留空，不得呈现为 0；合计按 0 计。"""
    payload = _payload(
        lines=[
            _line(poNo="PO3", poLineNo="30", openQty=None),
            _line(poNo="PO4", poLineNo="40", openQty=Decimal("60")),
        ]
    )
    blob = renderChaseDocx(payload)
    rows = _docxTableRows(blob)
    assert rows[1][3] == "", "数量未知 ⇒ 单元格留空（不是 0）"
    assert rows[2][3] == "60"
    xml = _docxText(blob)
    assert "合计欠交 60" in xml, "未知量不贡献欠交量"


def test_excel_unknown_open_qty_cell_is_empty() -> None:
    """Excel 侧同理：NULL 数量落空单元格，不写 0。"""
    payload = _payload(lines=[_line(openQty=None)])
    book = load_workbook(io.BytesIO(renderChaseExcel(payload)))
    sheet = book["催货明细"]
    assert sheet.cell(row=2, column=7).value is None, "欠交量 NULL ⇒ 空单元格"


# ---------------------------------------------------------------------------
# Excel 公式注入（对外材料的安全红线）
# ---------------------------------------------------------------------------

_FORMULA_PAYLOAD = '=HYPERLINK("http://evil","click")'


def test_excel_detail_cell_with_formula_is_stored_as_text() -> None:
    """物料描述以 ``=`` 开头 ⇒ **不得**变成活公式（openpyxl 写入时判定为 ``data_type='f'``）。

    供应商在 Excel 里打开即执行 ⇒ 对外材料的安全事故。
    修法：写入后强制 ``data_type='s'``（内容原样，Excel 按文本存储）。
    """
    payload = _payload(lines=[_line(materialDesc=_FORMULA_PAYLOAD)])
    book = load_workbook(io.BytesIO(renderChaseExcel(payload)))
    cell = book["催货明细"].cell(row=2, column=4)  # 物料描述列
    assert cell.data_type == "s", f"公式串必须按文本存储，实际 data_type={cell.data_type!r}"
    assert cell.value == _FORMULA_PAYLOAD, "内容必须原样保留（不能被转义/截断）"


def test_excel_info_cell_with_formula_is_stored_as_text() -> None:
    """信息 Sheet（抬头）同样受保护 —— 备注 / 单号也是外部数据。"""
    payload = _payload(notes=_FORMULA_PAYLOAD, chaseDraftId="=1+1")
    book = load_workbook(io.BytesIO(renderChaseExcel(payload)))
    info = book["催货单信息"]
    values = {info.cell(row=r, column=1).value: info.cell(row=r, column=2) for r in range(1, 8)}
    for label in ("备注", "催货单号"):
        cell = values[label]
        assert cell.data_type == "s", f"{label} 单元格 data_type={cell.data_type!r}（应为 s）"
    assert values["备注"].value == _FORMULA_PAYLOAD
    assert values["催货单号"].value == "=1+1"


def test_excel_numeric_and_boolean_cells_keep_their_type() -> None:
    """只压字符串的 data_type —— 数字/布尔仍是数字/布尔（不能被写成文本）。"""
    payload = _payload(lines=[_line(orderedQty=Decimal("100"), isDirty=False)])
    book = load_workbook(io.BytesIO(renderChaseExcel(payload)))
    sheet = book["催货明细"]
    assert sheet.cell(row=2, column=5).data_type == "n", "订购量是数字"
    assert sheet.cell(row=2, column=10).data_type == "b", "脏数据标记是布尔"


# ---------------------------------------------------------------------------
# 对外时间口径（业务时区 UTC+8）
# ---------------------------------------------------------------------------


def test_deadline_rendered_in_business_timezone() -> None:
    """UTC 墙钟直接渲染会让**对外承诺期限早 8 小时** ⇒ 必须转业务时区并标注。"""
    payload = _payload(expectedReplyDeadline=datetime(2026, 12, 1, 23, 59, 59, tzinfo=UTC))
    book = load_workbook(io.BytesIO(renderChaseExcel(payload)))
    info = book["催货单信息"]
    values = {info.cell(row=r, column=1).value: info.cell(row=r, column=2).value for r in range(1, 8)}
    deadline = values["回复期限"]
    assert deadline.startswith("2026-12-02 07:59"), (
        f"UTC 23:59 ⇒ 业务时区应为 2026-12-02 07:59，实际 {deadline!r}"
    )
    assert "UTC+8" in deadline, f"对外文件必须标注时区，实际 {deadline!r}"


def test_docx_deadline_uses_business_timezone_and_labels_it() -> None:
    """Word 侧同口径：期限按业务时区渲染 + 时区标注。"""
    payload = _payload(expectedReplyDeadline=datetime(2026, 12, 1, 23, 59, 59, tzinfo=UTC))
    xml = _docxText(renderChaseDocx(payload))
    assert "2026-12-02 07:59" in xml, "回复期限应按业务时区（+08:00）渲染"
    assert "UTC+8" in xml, "对外文件必须标注时区"
    assert "2026-12-01 23:59" not in xml, "不得出现 UTC 墙钟"


def test_docx_signature_date_uses_business_timezone() -> None:
    """落款日期同源 ⇒ 也按业务时区（UTC 深夜落款可能差一天）。"""
    payload = _payload(createdTime=datetime(2026, 11, 30, 20, 0, tzinfo=UTC))
    xml = _docxText(renderChaseDocx(payload))
    assert "日期：2026-12-01" in xml, "UTC 2026-11-30 20:00 ⇒ 业务时区 2026-12-01"


# ---------------------------------------------------------------------------
# 合计口径：数量未知不得被陈述成「没有欠交」
# ---------------------------------------------------------------------------


def test_all_unknown_open_qty_does_not_claim_zero_owed() -> None:
    """全部数量未知 ⇒ **不得**写「合计欠交 0」（那是向供应商断言没有欠交）。"""
    payload = _payload(
        lines=[_line(poNo="PO-A", openQty=None), _line(poNo="PO-B", openQty=None)]
    )
    xml = _docxText(renderChaseDocx(payload))
    assert "数量待确认" in xml, "必须说明数量待确认"
    assert "合计欠交 0" not in xml, (
        f"全部数量未知时写「合计欠交 0」＝ 失实陈述，实际文案：{xml}"
    )


def test_partially_unknown_open_qty_states_pending_count() -> None:
    """部分未知 ⇒ 合计照实写，并显式说明有几行数量待确认、未计入。"""
    payload = _payload(
        lines=[_line(poNo="PO-A", openQty=None), _line(poNo="PO-B", openQty=Decimal("60"))]
    )
    xml = _docxText(renderChaseDocx(payload))
    assert "合计欠交 60（其中 1 行数量待确认，未计入）。" in xml


def test_all_known_open_qty_keeps_plain_total() -> None:
    """全部已知 ⇒ 保持原口径（不多余说明）。"""
    xml = _docxText(renderChaseDocx(_payload()))
    assert "本次催货单共 1 行订单明细，合计欠交 60。" in xml


# ---------------------------------------------------------------------------
# 供应商抬头：Excel 与 Word 同一口径
# ---------------------------------------------------------------------------


def test_supplier_header_is_consistent_between_excel_and_docx() -> None:
    """有名字 ⇒ ``名字（编码）``；无名字 ⇒ **只显示编码**（不能是 ``F109（F109）``）。"""
    withName = _payload()
    excel = load_workbook(io.BytesIO(renderChaseExcel(withName)))
    info = excel["催货单信息"]
    excelValue = info.cell(row=2, column=2).value
    docxXml = _docxText(renderChaseDocx(withName))
    assert excelValue == "上海纳特（F109）"
    assert "供应商：上海纳特（F109）" in docxXml, "Word 必须与 Excel 同口径"

    withoutName = _payload(supplierName=None)
    excel = load_workbook(io.BytesIO(renderChaseExcel(withoutName)))
    info = excel["催货单信息"]
    assert info.cell(row=2, column=2).value == "F109", "无名字时只显示编码，不能是 F109（F109）"
    docxXml = _docxText(renderChaseDocx(withoutName))
    assert "供应商：F109" in docxXml
    assert "F109（F109）" not in docxXml, "供应商名冗余"
