"""语义发布只读 API 的 DTO（P1b / D9）。

字段名 snake_case、JSON 别名 camelCase，与仓库全部 DTO 一致（`CamelModel`）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.domain.schemas import CamelModel


class SemanticReleaseSummaryRead(CamelModel):
    """列表行：只有元数据，不带 ~1MB 的快照正文。"""

    id: int
    version: str
    domain_code: str | None
    status: str
    schema_version: str
    manifest_hash: str
    created_by: str | None
    created_time: datetime
    activated_time: datetime | None
    note: str | None


class SemanticReleasePage(CamelModel):
    rows: list[SemanticReleaseSummaryRead]
    total: int


class SemanticReleaseSnapshotRead(CamelModel):
    """单条发布详情（详情页 / 激活快照 / 回滚目标预览）。"""

    id: int
    version: str
    domain_code: str | None
    status: str
    schema_version: str
    manifest_hash: str
    activated_time: datetime | None
    manifest: dict[str, Any]


class SemanticReleaseCreateRequest(CamelModel):
    """从当前治理表编译一份 DRAFT 快照。"""

    version: str = Field(min_length=1, max_length=32)
    domain_code: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=2000)


class SemanticReleaseOperationRead(CamelModel):
    """写操作结果：落库后的汇总 + 本次发布闸门报告。

    只有 `createDraft` 的响应可能带非空 `blockers`：DRAFT 允许带着阻断项落库
    （评审要看被拦下的内容）。`markValidated` / `activate` / `archive` 在阻断项
    非空时直接拒绝，所以它们的 `blockers` 恒为空，`warnings` 才是治理债清单。
    """

    release: SemanticReleaseSummaryRead
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
