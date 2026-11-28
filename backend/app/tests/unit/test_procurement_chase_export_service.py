"""催货单导出服务：payload 拼装 + Excel/Word 渲染。"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from decimal import Decimal

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
        lines=[
            {
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
        ],
        decisions=[],
    )
    base.update(overrides)
    return ChaseExportPayload(**base)


def _docxText(blob: bytes) -> str:
    """从 docx（zip）里取 word/document.xml 文本。"""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return archive.read("word/document.xml").decode("utf-8")


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


def test_build_payload_tolerates_missing_optional_fields() -> None:
    class _MinimalDraft:
        chase_draft_id = "d-2"
        status = "DRAFT"
        supplier_code = "F200"
        created_by = "buyer"
        created_time = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)

    payload = buildChaseExportPayload(
        draft=_MinimalDraft(),
        lines=[],
        decisions=[],
        supplierName=None,
        companyName=None,
    )
    assert payload.expectedReplyDeadline is None
    assert payload.notes is None
    assert payload.lines == []


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
    assert "60" in xml


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
            {
                "poNo": "PO2",
                "poLineNo": "20",
                "materialCode": "M2",
                "materialDesc": None,
                "orderedQty": Decimal("10"),
                "receivedQty": Decimal("10"),
                "openQty": Decimal("0"),
                "expectedDate": None,
                "overdueDays": 0,
                "isDirty": False,
            }
        ]
    )
    xml = _docxText(renderChaseDocx(payload))
    assert "PO2" in xml
    assert "合计欠交 0" in xml
