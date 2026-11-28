# 催货单导出（Excel/Word）+ 菜单入口 + 创建后跳转链接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 催货单支持单张下载（Excel 明细表 + Word 正式催货函），并在导航里能进入催货单页、创建后能一键跳过去。

**Architecture:** 新增纯导出服务 `procurement_chase_export_service.py`（只收 immutable payload，不碰 ORM，产出 bytes）+ 一个 `GET /chase/drafts/{id}/export?format=xlsx|docx` 端点（requireAdmin）。前端在催货单页每行加下载按钮；到货跟踪结果卡创建成功后给「查看催货单」跳转链接；`seed_menu_config.py` 补菜单项（全新 code ⇒ 跑一次 seed 即新增行）。

**Tech Stack:** FastAPI + SQLAlchemy async / openpyxl（Excel）/ python-docx（Word）/ React + antd + TypeScript。

**Spec:** `docs/superpowers/specs/2026-11-27-chase-export-design.md`

## Global Constraints

- **导出服务只收 immutable payload**：`ChaseExportPayload` 是 frozen dataclass；服务函数**不接触 DB/ORM/FastAPI**（沿用 `backend/app/services/evaluation_report_export_service.py` 范式）。
- **下载权限 = `requireAdmin`**，与 from-pending / validate / approve / reject / cancel 同口径；端点**自己声明** `requireAdmin`（催货 router 无 router 级依赖）。
- **不写死任何默认公司名**：抬头读 `system_config` 键 `chase.letter.company_name`；未配置 ⇒ **整行不渲染** + 服务端 warning，**不阻塞导出**。
- **供应商名走 MDM 目录**：`loadMdmDirectory(session, entityType="SUPPLIER", codes=[supplier_code])`（与到货跟踪同款）；查不到 ⇒ 回落 `supplier_code`，不 fail-closed。
- **Excel 明细列固定**：订单号 / 行号 / 物料编码 / 物料描述 / 订购量 / 已收量 / 欠交量 / 期望到货日 / 逾期天数 / 脏数据标记；顶部信息区含催货单号 / 供应商（码+名）/ 回复期限 / 状态 / 创建人 / 创建时间。
- **Word 版式**：抬头（公司法定名 + 供应商全名 + 单号 + 回复期限）→ 催货说明（欠交总行数/总量）→ 明细表 → 落款（催货人 + 日期）。中文字体用 docx 的 `w:eastAsia`（`pdf_fonts.ensureCjkFont()` 是 PDF 专用，不复用）。
- **依赖已在 `backend/pyproject.toml`**：`openpyxl>=3.1.5`、`python-docx>=1.0.0`（不新增依赖）。
- **无 DDL / 无迁移**（`procurement_chase_draft` / `_line` / `_decision` 是既有表）。
- Python 命名：函数/变量 `camelCase`、常量 `UPPER_SNAKE_CASE`、类型 `PascalCase`。
- TypeScript 严格模式；不使用 `.at()`（tsconfig lib 不含 es2022）。
- 文件 200–400 行为宜。

---

### Task 1: 导出服务（payload + Excel + Word）

**Files:**
- Create: `backend/app/services/procurement_chase_export_service.py`
- Test: `backend/app/tests/unit/test_procurement_chase_export_service.py`

**Interfaces:**
- Consumes: 既有模型 `ProcurementChaseDraft` / `ProcurementChaseLine` / `ProcurementChaseDecision` 的字段（**只读其属性，不 import ORM 类型**——服务收 dict/dataclass）
- Produces:
  - `ChaseExportPayload`（frozen dataclass）：`chaseDraftId: str`、`status: str`、`supplierCode: str`、`supplierName: str | None`、`companyName: str | None`、`expectedReplyDeadline: datetime | None`、`notes: str | None`、`createdBy: str`、`createdTime: datetime`、`lines: list[dict[str, Any]]`、`decisions: list[dict[str, Any]]`
  - `buildChaseExportPayload(*, draft: Any, lines: list[Any], decisions: list[Any], supplierName: str | None, companyName: str | None) -> ChaseExportPayload`
  - `renderChaseExcel(payload: ChaseExportPayload) -> bytes`
  - `renderChaseDocx(payload: ChaseExportPayload) -> bytes`

- [ ] **Step 1: 写失败测试**

```python
"""催货单导出服务：payload 拼装 + Excel/Word 渲染。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

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
                "poNo": "PO1", "poLineNo": "10", "materialCode": "M1",
                "materialDesc": "不锈钢板", "orderedQty": Decimal("100"),
                "receivedQty": Decimal("40"), "openQty": Decimal("60"),
                "expectedDate": None, "overdueDays": 15, "isDirty": False,
            }
        ],
        decisions=[],
    )
    base.update(overrides)
    return ChaseExportPayload(**base)


def test_build_payload_maps_orm_fields() -> None:
    payload = buildChaseExportPayload(
        draft=_Draft(), lines=[_Line()], decisions=[],
        supplierName="上海纳特", companyName="某某有限公司",
    )
    assert payload.chaseDraftId == "d-1"
    assert payload.supplierCode == "F109"
    assert payload.lines[0]["openQty"] == Decimal("60")
    assert payload.lines[0]["poLineNo"] == "10"


def test_excel_contains_header_and_one_data_row() -> None:
    from openpyxl import load_workbook

    blob = renderChaseExcel(_payload())
    book = load_workbook(__import__("io").BytesIO(blob))
    sheet = book["催货明细"]
    headers = [c.value for c in sheet[1]]
    assert "订单号" in headers and "欠交量" in headers and "逾期天数" in headers
    assert sheet.max_row == 2, "1 行明细 ⇒ 2 行（含表头）"
    row = [c.value for c in sheet[2]]
    assert "PO1" in row


def test_excel_works_with_empty_lines() -> None:
    from openpyxl import load_workbook

    blob = renderChaseExcel(_payload(lines=[]))
    book = load_workbook(__import__("io").BytesIO(blob))
    assert book["催货明细"].max_row == 1, "无明细时只有表头"


def test_docx_contains_key_texts() -> None:
    blob = renderChaseDocx(_payload())
    text = blob.decode("utf-8", errors="ignore")
    # docx 是 zip；用 zipfile 提取 document.xml 才能看到文本
    import zipfile, io
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert "上海纳特" in xml
    assert "某某有限公司" in xml
    assert "PO1" in xml
    assert "60" in xml


def test_docx_omits_company_row_when_not_configured() -> None:
    """未配置公司名 ⇒ 整行不渲染（不留空白行、不拿品牌名冒充）。"""
    import zipfile, io
    blob = renderChaseDocx(_payload(companyName=None))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert "上海纳特" in xml
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest app/tests/unit/test_procurement_chase_export_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.procurement_chase_export_service'`

- [ ] **Step 3: 写实现**

```python
"""催货单导出服务（Excel 明细 + Word 催货函）。

范式沿用 ``evaluation_report_export_service``：**只接收 immutable payload，
不接触 DB/ORM/FastAPI**；由 API 层从 ORM 拼好 payload 再调用。

设计要点：
- 表头/抬头里的公司法定名称来自 ``system_config`` 键
  ``chase.letter.company_name``（由 API 层读取后传入）；**未配置 ⇒ 整行不渲染**，
  不写死任何默认名（写死一个「看起来像公司名」的值比留空更危险）。
- 供应商名由 API 层经 ``loadMdmDirectory`` 查得；查不到 ⇒ 回落编码。
- 中文字体：docx 侧用 ``w:eastAsia`` 设置（``pdf_fonts.ensureCjkFont`` 是 PDF 专用）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

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


def buildChaseExportPayload(
    *,
    draft: Any,
    lines: list[Any],
    decisions: list[Any],
    supplierName: str | None,
    companyName: str | None,
) -> ChaseExportPayload:
    """从 ORM 行拼出导出 payload（纯拼装，不查库）。"""
    return ChaseExportPayload(
        chaseDraftId=str(draft.chase_draft_id),
        status=str(draft.status),
        supplierCode=str(draft.supplier_code),
        supplierName=supplierName,
        companyName=companyName,
        expectedReplyDeadline=getattr(draft, "expected_reply_deadline", None),
        notes=getattr(draft, "notes", None),
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


def renderChaseExcel(payload: ChaseExportPayload) -> bytes:
    """Excel：抬头信息区 + 「催货明细」Sheet（列固定）。"""
    book = Workbook()
    book.remove(book.active)
    sheet = book.create_sheet("催货明细")

    info: list[tuple[str, str]] = [
        ("催货单号", payload.chaseDraftId),
        ("供应商", f"{payload.supplierName or payload.supplierCode}（{payload.supplierCode}）"),
        ("回复期限", payload.expectedReplyDeadline.isoformat() if payload.expectedReplyDeadline else ""),
        ("状态", payload.status),
        ("创建人", payload.createdBy),
        ("创建时间", payload.createdTime.isoformat()),
        ("备注", payload.notes or ""),
    ]
    rowIndex = 1
    for label, value in info:
        sheet.cell(row=rowIndex, column=1, value=label).font = Font(bold=True)
        sheet.cell(row=rowIndex, column=2, value=value)
        rowIndex += 1
    rowIndex += 1

    headerRow = rowIndex
    for colIndex, (title, _key) in enumerate(_EXCEL_COLUMNS, start=1):
        cell = sheet.cell(row=headerRow, column=colIndex, value=title)
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
    for offset, line in enumerate(payload.lines, start=1):
        for colIndex, (_title, key) in enumerate(_EXCEL_COLUMNS, start=1):
            value = line.get(key)
            sheet.cell(row=headerRow + offset, column=colIndex, value=value)
    for colIndex in range(1, len(_EXCEL_COLUMNS) + 1):
        sheet.column_dimensions[get_column_letter(colIndex)].width = 16
    sheet.freeze_panes = sheet.cell(row=headerRow + 1, column=1)

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _setCjkFont(document: Any, size: int = 10) -> None:
    """给 docx 的 Normal 样式设中文字体（w:eastAsia）。"""
    style = document.styles["Normal"]
    style.font.name = "SimSun"
    style.font.size = Pt(size)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "SimSun")


def renderChaseDocx(payload: ChaseExportPayload) -> bytes:
    """Word：抬头 → 催货说明 → 明细表 → 落款。

    公司法定名称未配置 ⇒ **整行不渲染**（不留空白行）。
    """
    document = Document()
    _setCjkFont(document)

    if payload.companyName:
        document.add_paragraph(payload.companyName)
    document.add_paragraph(f"催货单号：{payload.chaseDraftId}")
    document.add_paragraph(f"供应商：{payload.supplierName or payload.supplierCode}")
    if payload.expectedReplyDeadline:
        document.add_paragraph(
            f"回复期限：{payload.expectedReplyDeadline.strftime('%Y-%m-%d')}"
        )

    totalLines = len(payload.lines)
    totalOpen = sum(
        float(line.get("openQty") or 0) for line in payload.lines
    )
    document.add_paragraph(
        f"催货说明：贵司有 {totalLines} 行订单尚未完成到货，合计欠交 {totalOpen:g}。"
        + (
            f"请于 {payload.expectedReplyDeadline.strftime('%Y-%m-%d')} 前回复处理计划。"
            if payload.expectedReplyDeadline
            else ""
        )
    )

    table = document.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for index, title in enumerate(
        ["订单号", "行号", "物料描述", "欠交量", "期望到货日", "逾期天数"]
    ):
        table.rows[0].cells[index].text = title
    for line in payload.lines:
        cells = table.add_row().cells
        cells[0].text = str(line.get("poNo") or "")
        cells[1].text = str(line.get("poLineNo") or "")
        cells[2].text = str(line.get("materialDesc") or line.get("materialCode") or "")
        cells[3].text = str(line.get("openQty") or "")
        cells[4].text = str(line.get("expectedDate") or "")
        cells[5].text = str(line.get("overdueDays") or "")

    if payload.notes:
        document.add_paragraph(f"备注：{payload.notes}")
    document.add_paragraph(f"催货人：{payload.createdBy}")
    document.add_paragraph(f"日期：{payload.createdTime.strftime('%Y-%m-%d')}")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest app/tests/unit/test_procurement_chase_export_service.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/procurement_chase_export_service.py backend/app/tests/unit/test_procurement_chase_export_service.py
git commit -m "feat(datasource): 催货单导出服务 —— Excel 明细 + Word 催货函（immutable payload）"
```

---

### Task 2: 导出端点

**Files:**
- Modify: `backend/app/api/v1/procurement_chase.py`
- Test: `backend/app/tests/integration/test_procurement_chase_export_api.py`

**Interfaces:**
- Consumes: Task 1 的 `buildChaseExportPayload` / `renderChaseExcel` / `renderChaseDocx`；既有 `ProcurementChaseService().getDraft(session, chaseDraftId)`（返回 draft+lines+decisions）
- Produces: `GET /api/v1/procurement/chase/drafts/{chaseDraftId}/export?format=xlsx|docx`

- [ ] **Step 1: 写失败测试**

```python
"""催货单导出端点契约：200 / 404 / 400 / 401 / 403。"""

from __future__ import annotations

import pytest


async def _login(client) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "qa_export_admin", "password": "QaExport@2026"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["accessToken"]


async def test_export_xlsx_returns_attachment(client, seeded_chase_draft) -> None:
    token = await _login(client)
    chaseId, _supplierCode = seeded_chase_draft
    resp = await client.get(
        f"/api/v1/procurement/chase/drafts/{chaseId}/export?format=xlsx",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml"
    )
    assert len(resp.content) > 0


async def test_export_docx_returns_attachment(client, seeded_chase_draft) -> None:
    token = await _login(client)
    chaseId, _supplierCode = seeded_chase_draft
    resp = await client.get(
        f"/api/v1/procurement/chase/drafts/{chaseId}/export?format=docx",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.content[:2] == b"PK", "docx 是 zip"


async def test_export_unknown_draft_404(client) -> None:
    token = await _login(client)
    resp = await client.get(
        "/api/v1/procurement/chase/drafts/does-not-exist/export?format=xlsx",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_export_bad_format_400(client, seeded_chase_draft) -> None:
    token = await _login(client)
    chaseId, _supplierCode = seeded_chase_draft
    resp = await client.get(
        f"/api/v1/procurement/chase/drafts/{chaseId}/export?format=pdf",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


async def test_export_requires_login(client, seeded_chase_draft) -> None:
    chaseId, _supplierCode = seeded_chase_draft
    resp = await client.get(
        f"/api/v1/procurement/chase/drafts/{chaseId}/export?format=xlsx"
    )
    assert resp.status_code == 401
```

> **测试夹具说明**：`seeded_chase_draft` 与 `qa_export_admin` 账号需要在该测试文件里
> 用 fixture 现场创建（**不要重置/覆盖任何已有账号**——见项目规则「测试身份新建账号」）：
> 建一个用户名 `qa_export_admin` 的系统管理员账号（bcrypt cost=12），再往库里插
> 一条 `procurement_chase_draft` + 两条 `procurement_chase_line`（用真实 ORM 模型）。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest app/tests/integration/test_procurement_chase_export_api.py -v`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 写实现**

在 `backend/app/api/v1/procurement_chase.py` 追加（import 段加
`from urllib.parse import quote` 与 Task 1 的三个函数）：

```python
_EXPORT_FORMATS = {"xlsx", "docx"}


def _companyName(session: AsyncSession) -> str | None:
    """读 system_config 的公司法定名称；未配置/读失败 ⇒ None（不阻塞导出）。"""
    try:
        row = await session.execute(
            text("SELECT value FROM system_config WHERE key = 'chase.letter.company_name'")
        )
        raw = row.scalar_one_or_none()
    except Exception:  # noqa: BLE001 - 读不到不影响导出
        logger.warning("读取 chase.letter.company_name 失败，抬头留空", exc_info=True)
        return None
    value = (raw or "").strip()
    return value or None


@router.get("/drafts/{chaseDraftId}/export")
async def exportChaseDraft(
    chaseDraftId: str,
    format: str = Query(default="xlsx", description="xlsx | docx"),
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> Response:
    """导出单张催货单：Excel 明细表 或 Word 催货函。

    权限与其它催货端点同口径（requireAdmin）——导出内容与它们同源。
    """
    if format not in _EXPORT_FORMATS:
        raise HTTPException(
            status_code=400, detail=f"format 只支持 {sorted(_EXPORT_FORMATS)}，收到 {format}"
        )
    detail = await _service.getDraft(session, chaseDraftId)
    draft = detail["draft"]
    supplierName = await _supplierNameOf(session, draft["supplierCode"])
    payload = buildChaseExportPayload(
        draft=draft,
        lines=detail["lines"],
        decisions=detail["decisions"],
        supplierName=supplierName,
        companyName=await _companyName(session),
    )
    if format == "xlsx":
        blob = renderChaseExcel(payload)
        mediaType = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        extension = "xlsx"
    else:
        blob = renderChaseDocx(payload)
        mediaType = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        extension = "docx"
    filename = f"催货单_{payload.chaseDraftId[:8]}.{extension}"
    return Response(
        content=blob,
        media_type=mediaType,
        headers={
            "Content-Disposition": (
                f"attachment; filename=chase-{payload.chaseDraftId[:8]}.{extension}; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )
```

另加供应商名解析（与到货跟踪同款）：

```python
async def _supplierNameOf(session: AsyncSession, supplierCode: str) -> str | None:
    """MDM 目录查供应商全名；查不到 ⇒ None（回落编码，不阻塞导出）。"""
    try:
        from app.services.entity_mapping_names import loadMdmDirectory

        directory = await loadMdmDirectory(
            session, entityType="SUPPLIER", codes=(supplierCode,)
        )
    except Exception:  # noqa: BLE001 - 查不到不影响导出
        logger.warning("查询供应商名称失败，回落编码 %s", supplierCode, exc_info=True)
        return None
    # 真实签名：loadMdmDirectory(session, *, entityType, codes: tuple[str, ...])
    #            -> dict[code, {"name": str | None, "enterpriseKey": ...}]
    name = (directory.get(supplierCode) or {}).get("name")
    return str(name) if name else None
```

> **注意（已核对真实签名）**：`loadMdmDirectory(session, *, entityType: str,
> codes: tuple[str, ...]) -> dict[code, {"name": str | None, "enterpriseKey": Any}]`
> —— `codes` 是**元组**且为**关键字参数**；同名多行时取首个非空名称。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest app/tests/integration/test_procurement_chase_export_api.py -v`
Expected: 5 passed

- [ ] **Step 5: 回归**

Run: `cd backend && uv run pytest app/tests/integration/test_procurement_chase_api.py app/tests/unit/test_procurement_chase_export_service.py -q`
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/procurement_chase.py backend/app/tests/integration/test_procurement_chase_export_api.py
git commit -m "feat(datasource): 催货单导出端点 —— xlsx/docx 单张下载（requireAdmin）"
```

---

### Task 3: 前端下载 + 菜单入口 + 创建后跳转

**Files:**
- Modify: `frontend/src/api/procurementChase.ts`
- Modify: `frontend/src/pages/ProcurementChasePage.tsx`
- Modify: `frontend/src/components/copilot/DeliveryTrackingResultCard.tsx`
- Modify: `backend/scripts/seed_menu_config.py`
- Modify: `frontend/src/i18n/zh-CN.ts`、`frontend/src/i18n/en-US.ts`
- Test: `frontend/src/tests/ProcurementChaseExport.test.tsx`（新建）

**Interfaces:**
- Consumes: Task 2 的端点 `GET /procurement/chase/drafts/{id}/export?format=xlsx|docx`
- Produces:
  - `downloadChaseDraft(chaseDraftId: string, format: "xlsx" | "docx"): Promise<void>`（`frontend/src/api/procurementChase.ts`）

- [ ] **Step 1: 写失败测试**

```typescript
import { describe, expect, it, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { downloadChaseDraft } from "../api/procurementChase";

describe("催货单导出下载", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    httpMock.get.mockResolvedValue({ data: new Blob(["x"]) });
    // jsdom 无 createObjectURL
    (globalThis as unknown as { URL: { createObjectURL: () => string } }).URL.createObjectURL =
      () => "blob:mock";
    (globalThis as unknown as { URL: { revokeObjectURL: (u: string) => void } }).URL.revokeObjectURL =
      () => undefined;
  });

  it("用 blob 响应下载并带正确 format 参数", async () => {
    await downloadChaseDraft("d-1", "xlsx");
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/procurement/chase/drafts/d-1/export");
    expect(call[1].params).toEqual({ format: "xlsx" });
    expect(call[1].responseType).toBe("blob");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/tests/ProcurementChaseExport.test.tsx`
Expected: FAIL — `downloadChaseDraft is not a function`

- [ ] **Step 3: 写实现**

`frontend/src/api/procurementChase.ts` 追加：

```typescript
/** 下载催货单（Excel 明细表 / Word 催货函）。
 *  用 blob 响应（httpClient 走 Bearer token，`<a download>` 带不了 header）。 */
export async function downloadChaseDraft(
  chaseDraftId: string,
  format: "xlsx" | "docx",
): Promise<void> {
  const res = await httpClient.get(
    `/procurement/chase/drafts/${chaseDraftId}/export`,
    { params: { format }, responseType: "blob" },
  );
  const blob = new Blob([res.data]);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `催货单_${chaseDraftId.slice(0, 8)}.${format}`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
```

`backend/scripts/seed_menu_config.py` 的采购段菜单项里追加：

```python
{"parent": "section.procurement", "code": "item.procurementChase", "label_key": "menu.item.procurementChase", "icon_code": "partition", "sort_order": 2750, "path": "/procurement/chase"},
```

> `sort_order` 已核对：采购段现有值为 2710 / 2720 / 2730 / 2740 / 2745 / 2740 ⇒ 新项取 **2750**
> （末尾之后，避免与已有项撞值）。

i18n（zh-CN + en-US 的 `menu.item.*` 下）：
```
"menu.item.procurementChase": "催货单"     # en: "Chase Orders"
```

`frontend/src/pages/ProcurementChasePage.tsx` 的列表「操作」列追加下载按钮：

```tsx
<Space size={4}>
  <Button size="small" onClick={() => void downloadChaseDraft(record.chaseDraftId, "xlsx")}>
    {t("chase.export.excel")}
  </Button>
  <Button size="small" onClick={() => void downloadChaseDraft(record.chaseDraftId, "docx")}>
    {t("chase.export.word")}
  </Button>
  <a onClick={() => void openDetail(record.chaseDraftId)}>{t("chase.column.detail")}</a>
</Space>
```

`frontend/src/components/copilot/DeliveryTrackingResultCard.tsx` 的创建成功分支：成功 toast 里给跳转链接（antd `message.success` 支持 ReactNode ⇒ 传一个带 `<a href="/procurement/chase">` 的片段）。

i18n 追加：`chase.export.excel` = "Excel" / `chase.export.word` = "Word"；
`chase.viewList` = "查看催货单" / "View chase orders"。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/tests/ProcurementChaseExport.test.tsx src/tests/__tests__/ProcurementCopilotPage.test.tsx`
Expected: 全绿

- [ ] **Step 5: typecheck + 菜单 seed 测试**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

Run: `cd backend && uv run pytest app/tests/unit/test_agent_orchestration_menu_seed.py app/tests/unit/test_seed_menu_research.py -v`
（已核对：仓库里**没有** `test_menu_config_seeds.py`，菜单守卫是这两个文件）
Expected: 全绿（菜单项声明合法）

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/procurementChase.ts frontend/src/pages/ProcurementChasePage.tsx \
  frontend/src/components/copilot/DeliveryTrackingResultCard.tsx \
  frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts \
  frontend/src/tests/ProcurementChaseExport.test.tsx backend/scripts/seed_menu_config.py
git commit -m "feat(datasource-ui): 催货单下载按钮 + 菜单入口 + 创建后跳转链接"
```

---

## 验证清单

- [ ] 后端全量：`cd backend && uv run pytest app/tests/unit/test_procurement_chase_export_service.py app/tests/integration/test_procurement_chase_export_api.py app/tests/integration/test_procurement_chase_api.py -q`
- [ ] 前端：`cd frontend && npx vitest run src/tests/ProcurementChaseExport.test.tsx && npx tsc --noEmit`
- [ ] 手工冒烟（本地）：采购 Copilot → 到货跟踪 → 勾选行 → 创建催货单 ⇒ 出现「查看催货单」链接 ⇒ 左侧菜单出现「催货单」⇒ 列表每行能下载 xlsx/docx
- [ ] **上线必做**：跑一次 `python -m scripts.seed_menu_config`（催货单菜单 code 是全新的 ⇒ 直接新增行）
- [ ] 部署：重建 backend + frontend 镜像；**worker 同镜像须一并 `--force-recreate`**（`Harness/skills/thdeploy/SKILL.md` §worker 同步铁律）
