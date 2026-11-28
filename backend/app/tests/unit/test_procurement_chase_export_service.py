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
