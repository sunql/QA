"""P3 编译作业的 HTTP 契约。"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import Any
from pydantic import Field, field_validator
from app.domain.schemas import CamelModel

MAX_COMPILE_PAGE_IDS = 200

class WikiCompileCreateRequest(CamelModel):
    scope: str = Field(description="ALL / PAGE_IDS / DIMENSION")
    page_ids: list[str] | None = Field(default=None, max_length=MAX_COMPILE_PAGE_IDS)
    dimension: str | None = Field(default=None, max_length=30)
    model_id: int | None = Field(default=None)
    fallback_model_id: int | None = Field(default=None)

    @field_validator("page_ids")
    @classmethod
    def _dropBlankPageIds(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [item for item in value if item and item.strip()]

class WikiCompileTaskRead(CamelModel):
    id: int
    status: str
    scope: str | None
    selected_model_id: int | None
    fallback_model_id: int | None
    total_items: int
    success_items: int
    skipped_items: int
    failed_items: int
    total_cost_usd: Decimal
    error_message: str | None
    created_by_user_id: int | None
    created_time: datetime
    started_time: datetime | None
    heartbeat_time: datetime | None
    finished_time: datetime | None

class WikiCompileTaskListRead(CamelModel):
    items: list[WikiCompileTaskRead]
    total: int

class WikiCompileItemRead(CamelModel):
    id: int
    task_id: int
    page_id: str
    status: str
    mechanism_counts: dict[str, Any] | None
    attempt_count: int
    error_message: str | None
    started_time: datetime | None
    finished_time: datetime | None

class WikiClaimUpdateRequest(CamelModel):
    claim_text: str = Field(min_length=1, max_length=1000)
    @field_validator("claim_text")
    @classmethod
    def _rejectBlank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("claim_text cannot be blank")
        return value.strip()

class KnowledgeClaimDetailRead(CamelModel):
    id: int
    page_id: str
    claim_text: str
    claim_type: str | None
    subject_id: str | None
    predicate: str | None
    object_value: str | None
    object_type: str | None
    confidence: Decimal | None
    authority_level: str | None
    status: str | None
    triple_stale: bool
    created_time: datetime

__all__ = [
    "MAX_COMPILE_PAGE_IDS", "WikiCompileCreateRequest", "WikiCompileTaskRead",
    "WikiCompileTaskListRead", "WikiCompileItemRead", "WikiClaimUpdateRequest",
    "KnowledgeClaimDetailRead",
]
