"""催货单 DTO（feat/delivery-chase-closed-loop）。

JSON 契约用 camelCase（CamelModel）；ORM/服务层 snake_case（仓库约定，
刻意偏离 PEP 8）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ChaseModel(BaseModel):
    """催货单 DTO 基类（camelCase 别名，与 RFQ DTO 同形态）。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        protected_namespaces=(),
    )


class ChaseLineInput(ChaseModel):
    """`from-pending` 的行身份对：**只收身份，不收数量**（服务端回查）。"""

    po_no: str = Field(min_length=1, max_length=64)
    po_line: str = Field(min_length=1, max_length=64)


class ChaseFromPendingCreate(ChaseModel):
    """Copilot 选行 → 批量创建（按供应商分组，一个供应商一张催货单）。"""

    lines: list[ChaseLineInput] = Field(min_length=1, max_length=200)
    expected_reply_deadline: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ChaseLineRead(ChaseModel):
    """催货行（服务端回查后的快照）。"""

    po_no: str
    po_line: str
    material_code: str | None = None
    material_desc: str | None = None
    ordered_qty: Decimal | None = None
    received_qty: Decimal | None = None
    open_qty: Decimal | None = None
    expected_date: str | None = None
    overdue_days: int | None = None


class ChaseDecisionRead(ChaseModel):
    decision: str
    actor: str
    note: str | None
    decided_time: datetime | None


class ChaseDraftRead(ChaseModel):
    chase_draft_id: str
    version: int
    status: str
    request_id: str
    supplier_enterprise_key: int
    supplier_code: str
    expected_reply_deadline: datetime
    notes: str | None
    line_count: int
    created_by: str
    created_time: datetime
    updated_time: datetime


class ChaseDraftDetailRead(ChaseModel):
    """详情（含行 + 决策时间线）。"""

    draft: ChaseDraftRead
    lines: list[ChaseLineRead]
    decisions: list[ChaseDecisionRead]


class ChaseFromPendingResultRead(ChaseModel):
    """批量创建结果。"""

    request_id: str
    drafts: list[ChaseDraftRead]
    unresolved_suppliers: list[str] = Field(default_factory=list)
    skipped_lines: list[dict] = Field(default_factory=list)


class ChaseDecisionBody(ChaseModel):
    """决策 body：只有 note —— **actor 从登录身份取**（`requireAdmin` 的 userId），
    不收客户端传的 actor（可伪造；与 RFQ 的 `RfqDecisionBody` 同口径）。"""

    note: str | None = Field(default=None, max_length=2000)
