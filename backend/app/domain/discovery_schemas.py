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
    scope: str = Field(
        ..., description="比较范围：GLOBAL（全域清单）/ DOMAIN（该域切片，基线与候选同口径）"
    )
    baseline_domain_code: str | None = Field(
        default=None,
        description=(
            "基线所属数据域（DOMAIN 作用域下，**候选侧也按同一个域切片** ⇒ "
            "两侧同口径；GLOBAL 作用域下为 None）"
        ),
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


# ---------------------------------------------------------------- A2：运行


class DiscoveryRunRequest(CamelModel):
    """受理一次发现运行（剖析 + 可选候选生成）。

    **未知字段一律拒绝**：`source_id` 决定连哪条被授权的业务库。
    """

    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")

    source_id: int = Field(..., gt=0, description="发现源 id")
    snapshot_id: int | None = Field(
        default=None, gt=0, description="快照 id；不传 = 该源最新快照"
    )
    tables: list[str] | None = Field(
        default=None,
        max_length=50,
        description="只剖析这些表（不传 = 快照全部表；建议显式指定控制成本）",
    )
    with_candidates: bool = Field(
        default=True, description="剖析后是否继续调用 LLM 生成候选"
    )


class DiscoveryRunRead(CamelModel):
    """运行记录（含 LLM 计量）。"""

    id: int
    source_id: int
    snapshot_id: int | None
    trigger: str
    status: str
    model_version: str | None
    prompt_version: str | None
    prompt_tokens: int
    completion_tokens: int
    cost: float
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None
    created_by: str | None


# ---------------------------------------------------------------- A2：候选 / 问题


class DiscoveryCandidateRead(CamelModel):
    id: int
    run_id: int
    source_id: int
    snapshot_id: int
    candidate_type: str
    table_name: str | None
    column_name: str | None
    target_code: str | None
    payload: dict[str, Any]
    evidence_refs: list[Any]
    confidence: float | None
    model_version: str | None
    prompt_version: str | None
    status: str
    decided_by: str | None
    decided_time: datetime | None
    created_time: datetime


class DiscoveryCandidatePage(CamelModel):
    rows: list[DiscoveryCandidateRead]
    total: int


class DiscoveryFindingRead(CamelModel):
    id: int
    run_id: int
    source_id: int
    snapshot_id: int | None
    finding_type: str
    table_name: str | None
    column_name: str | None
    severity: str
    status: str
    detail: str
    evidence_refs: list[Any]
    created_time: datetime


class DiscoveryFindingPage(CamelModel):
    rows: list[DiscoveryFindingRead]
    total: int


# ---------------------------------------------------------------- A2：提案 / 审核


class DiscoveryProposalCreateRequest(CamelModel):
    """建提案草稿。`manifest` 是**候选后的完整 manifest**（不是补丁）。"""

    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")

    source_id: int = Field(..., gt=0, description="发现源 id")
    title: str = Field(..., min_length=1, max_length=200, description="提案标题")
    manifest: dict[str, Any] = Field(..., description="完整 manifest（必须满足 schemaVersion 契约）")
    base_release_id: int | None = Field(
        default=None, gt=0, description="基线发布 id；给了才算影响面"
    )
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH, description="备注")


class DiscoveryProposalRead(CamelModel):
    id: int
    title: str
    source_id: int
    base_release_id: int | None
    release_id: int | None
    diff_json: dict[str, Any]
    impact_json: dict[str, Any] | None
    status: str
    proposer: str
    created_time: datetime
    updated_time: datetime


class DiscoveryProposalPage(CamelModel):
    rows: list[DiscoveryProposalRead]
    total: int


class DiscoveryReviewRequest(CamelModel):
    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")

    decision: str = Field(..., description="APPROVE / REJECT / REQUEST_CHANGES")
    comment: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)
    approval_task_id: int | None = Field(
        default=None, gt=0, description="可选：P5 审批任务（须指向 ontology_proposal）"
    )


class DiscoveryReviewRead(CamelModel):
    id: int
    proposal_id: int
    reviewer: str
    decision: str
    comment: str | None
    approval_task_id: int | None
    created_time: datetime


class DiscoveryPublishRequest(CamelModel):
    """发布已批准的提案（走既有发布写口，**不新建发布通道**）。"""

    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")

    version: str = Field(..., min_length=1, max_length=32, description="新发布版本号（作用域内唯一）")
    domain_code: str | None = Field(
        default=None, max_length=64, description="发布作用域；不传 = 全局"
    )
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


class DiscoveryPublishRead(CamelModel):
    proposal_id: int
    proposal_status: str
    release_id: int
    version: str
    status: str
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DiscoveryMonitorRead(CamelModel):
    """一次增量监测的结论。"""

    source_id: int
    snapshot_id: int
    changed: bool
    run_id: int | None


class DiscoveryEvaluateRequest(CamelModel):
    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")

    dataset_version: str = Field(..., min_length=1, max_length=64, description="评测数据集版本")
    proposal_id: int | None = Field(default=None, gt=0, description="可选：关联提案")
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


class DiscoveryEvaluationRead(CamelModel):
    id: int
    proposal_id: int | None
    dataset_version: str
    metrics_json: dict[str, Any]
    verdict: str
    note: str | None
    created_by: str | None
    created_time: datetime


class DiscoveryEvaluationPage(CamelModel):
    rows: list[DiscoveryEvaluationRead]
    total: int
