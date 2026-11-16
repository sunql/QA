"""本体发现域 DTO（WP-ONT-AI A1）。

字段名 snake_case、JSON 别名 camelCase，与仓库全部 DTO 一致（`CamelModel`）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from app.domain.schemas import CamelModel

# 备注上限（字符）：备注是自由文本，但不该是数据通道。
MAX_NOTE_LENGTH = 2000


# ---------------------------------------------------------------- 发现源


class DiscoverySourceCreateRequest(CamelModel):
    """登记发现源（引用既有 `data_source.id`，**不新建连接**）。

    **未知字段一律拒绝**（`extra="forbid"`）：`datasource_id` 是授权根
    （改它 = 换一条被授权的连接），静默忽略会让调用方以为改成功了。
    """

    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")

    name: str = Field(..., min_length=1, max_length=100, description="发现源名称（唯一）")
    datasource_id: int = Field(..., gt=0, description="既有业务数据源 id")
    owner: str = Field(..., min_length=1, max_length=100, description="责任人（业务 / 数据 Owner）")
    access_policy: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "访问策略：mode=READ_ONLY（唯一允许值）、schema_owner=库模式属主。"
            "未知键一律拒绝（不静默忽略）"
        ),
    )
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH, description="备注")


class DiscoverySourceUpdateRequest(CamelModel):
    """更新发现源（停用 / 改责任人 / 改策略 / 改备注）。

    **未知字段一律拒绝**（`extra="forbid"`）：`datasource_id` 是授权根（改它 =
    换一条被授权的连接），静默忽略会让调用方以为改成功了。
    """

    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")

    status: str | None = Field(default=None, description="ACTIVE / DISABLED")
    owner: str | None = Field(
        default=None, min_length=1, max_length=100, description="责任人（业务 / 数据 Owner）"
    )
    access_policy: dict[str, Any] | None = Field(
        default=None,
        description="访问策略：mode=READ_ONLY（唯一允许值）、schema_owner=库模式属主",
    )
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH, description="备注")


class DiscoverySourceRead(CamelModel):
    """发现源读模型。"""

    id: int
    name: str
    datasource_id: int
    owner: str
    access_policy: dict[str, Any]
    status: str
    note: str | None
    created_by: str | None
    created_time: datetime
    snapshot_count: int = 0


class DiscoverySourcePage(CamelModel):
    rows: list[DiscoverySourceRead]
    total: int


# ---------------------------------------------------------------- 快照


class DiscoverySnapshotSummaryRead(CamelModel):
    """快照摘要（不含内容）。"""

    id: int
    source_id: int
    schema_hash: str
    table_count: int
    column_count: int
    captured_at: datetime
    created_by: str | None


class DiscoverySnapshotRead(DiscoverySnapshotSummaryRead):
    """快照详情（含内容）。"""

    snapshot: dict[str, Any]


class DiscoverySnapshotPage(CamelModel):
    rows: list[DiscoverySnapshotSummaryRead]
    total: int


class SnapshotCaptureRead(CamelModel):
    """一次采集的结果。"""

    snapshot_id: int
    source_id: int
    schema_hash: str
    table_count: int
    column_count: int
    captured_at: datetime
    reused: bool = Field(..., description="True = 内容未变，复用既有快照行")


# ---------------------------------------------------------------- 差异


class SemanticChangeRead(CamelModel):
    """一条结构化差异。"""

    kind: str
    severity: str
    risk_level: str
    section: str
    target: str
    field: str | None = None
    before: Any = None
    after: Any = None


class SemanticDiffRead(CamelModel):
    """基线发布 vs 当前治理表的差异。

    覆盖度必须**随结果一起披露**（`covered_sections` / `uncovered_sections` /
    `partial`）：本引擎只比较 classes / properties / joins，metrics / kpis /
    domains / businessObjects 未覆盖 ⇒ `has_breaking_changes=false` 只在
    「已覆盖分区内无破坏性变更」这个范围内成立。
    """

    baseline_release_id: int | None = None
    baseline_version: str | None = None
    baseline_hash: str | None = None
    scope: str = Field(..., description="比较范围：GLOBAL（全域清单）")
    baseline_domain_code: str | None = Field(
        default=None, description="基线所属数据域（仅用于定位基线，不裁剪候选侧）"
    )
    change_count: int
    total_change_count: int = Field(..., description="变更总数（未截断）")
    truncated: bool = Field(..., description="changes 是否被截断（超出上限）")
    dropped_breaking_count: int = Field(
        default=0,
        description="因截断而**未展示**的破坏性变更条数（>0 ⇒ 破坏性变更没看全）",
    )
    breaking_count: int
    additive_count: int
    cosmetic_count: int
    has_breaking_changes: bool
    overall_risk_level: str
    covered_sections: list[str] = Field(default_factory=list)
    uncovered_sections: list[str] = Field(default_factory=list)
    covered_fields: dict[str, list[str]] = Field(
        default_factory=dict,
        description="本次比较覆盖到的**字段级**范围（分区 → 字段名）",
    )
    partial: bool = Field(..., description="True = 未覆盖全部分区，不能据此断言无破坏性变更")
    changes: list[SemanticChangeRead]
