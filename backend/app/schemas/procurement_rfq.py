"""M5-3 询价草稿 API DTO（camelCase 线格式，设计稿 §8.6/§8.5）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.domain.schemas import CamelModel


class RfqLineItem(CamelModel):
    """询价行：物料 + 数量 + 单位 + 交期。"""

    materialKey: int
    quantity: int = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)
    deliveryDate: str = Field(min_length=8, max_length=10)


class RfqDraftCreate(CamelModel):
    """创建询价草稿请求体（Idempotency-Key 走请求头）。"""

    requestId: str = Field(min_length=1, max_length=64)
    deadline: datetime
    supplierKeys: list[int] = Field(default_factory=list, max_length=50)
    lineItems: list[RfqLineItem] = Field(default_factory=list, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)


class RfqDecisionRead(CamelModel):
    """决策留痕（DoD 4 审计）。"""

    decision: str
    actor: str
    note: str | None = None
    decidedTime: datetime | None = None


class RfqDraftRead(CamelModel):
    """询价草稿视图。"""

    rfqDraftId: UUID
    version: int
    status: str
    requestId: str
    deadline: datetime
    notes: str | None = None
    supplierKeys: list[int] = Field(default_factory=list)
    lineItems: list[RfqLineItem] = Field(default_factory=list)
    createdBy: str | None = None
    decisions: list[RfqDecisionRead] = Field(default_factory=list)


class RfqExpectedVersionBody(CamelModel):
    """带乐观锁的动作请求体（validate/submit/approve/reject/cancel 共用基座）。"""

    expectedVersion: int | None = Field(default=None, ge=1)


class RfqDecisionBody(RfqExpectedVersionBody):
    """审批决策请求体。"""

    note: str | None = Field(default=None, max_length=2000)


class RfqSendBody(RfqExpectedVersionBody):
    """发送询价请求体（M6-1）：``confirm=true`` = 二次确认（防误触）。"""

    confirm: bool = False
