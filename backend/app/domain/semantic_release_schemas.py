"""语义发布只读 API 的 DTO（P1b / D9）。

字段名 snake_case、JSON 别名 camelCase，与仓库全部 DTO 一致（`CamelModel`）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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
