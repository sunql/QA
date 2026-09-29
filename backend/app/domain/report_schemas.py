"""M4 Report 模板（A8）DTO。

请求/响应契约（camelCase 别名由 CamelModel 统一提供）；
ORM 行经 ``model_validate`` 读取（from_attributes 已在基类开启）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.domain.schemas import CamelModel

ReportStatus = Literal["PENDING_REVIEW", "APPROVED", "REJECTED"]


class ReportParamField(CamelModel):
    """模板参数 schema 单字段（前端动态表单渲染用）。"""

    name: str
    type: str
    required: bool
    pattern: str | None = None


class ReportTemplateOut(CamelModel):
    """模板元数据（GET /reports/templates）。"""

    code: str
    title: str
    description: str
    params_schema: list[ReportParamField] = Field(default_factory=list)


class ReportGenerateRequest(CamelModel):
    """POST /reports/generate 请求体。"""

    template_code: str = Field(min_length=1, max_length=50)
    params: dict[str, Any] = Field(default_factory=dict)


class ReportSectionOut(CamelModel):
    """渲染后的报告分节（JSONB 存储形态）。"""

    section_id: str
    title: str
    kind: Literal["table", "kpi_cards", "text"]
    data: Any = None
    render_error: str | None = None


class ReportInstanceRead(CamelModel):
    """报告实例详情。"""

    id: int
    template_code: str
    title: str
    params: dict[str, Any] | None = None
    sections: list[ReportSectionOut] = Field(default_factory=list)
    summary: str | None = None
    status: ReportStatus
    review_note: str | None = None
    reviewed_by: str | None = None
    created_by: str
    created_time: datetime | None = None
    updated_time: datetime | None = None


class ReportListItemRead(CamelModel):
    """报告列表项（轻量：不含 sections/params）。"""

    id: int
    template_code: str
    title: str
    status: ReportStatus
    created_by: str
    created_time: datetime | None = None


class ReportListPage(CamelModel):
    """分页信封（对齐 AuditLogPage 的 rows/total 口径）。"""

    rows: list[ReportListItemRead] = Field(default_factory=list)
    total: int = 0


class ReportReviewRequest(CamelModel):
    """POST /reports/{id}/review 请求体（admin only）。"""

    decision: Literal["APPROVE", "REJECT"]
    note: str | None = None
