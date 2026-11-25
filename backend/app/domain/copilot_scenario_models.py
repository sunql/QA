"""copilot 场景配置（P4，2026-11-24）。

场景从硬编码（ProcurementCopilotPage.tsx）变为 DB 业务资产：
卡片墙/工作区/提问模板/结果渲染器由此表驱动；加场景 = 插一行配置。

设计文档：docs/superpowers/specs/2026-11-24-copilot-scenario-config-design.md
"""

from __future__ import annotations

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntPk, TimestampMixin

#: 结果渲染器白名单（fail-closed：未知渲染器不渲染）。
RESULT_RENDERER_WHITELIST = frozenset(
    {"pending_delivery", "supplier_risk", "delivery_tracking", "generic_table", "none"}
)

#: 槽位类型白名单（未知 type 的槽位前端不渲染）。
SLOT_TYPE_WHITELIST = frozenset(
    {"number", "dateRange", "entityMulti", "dictMulti", "text"}
)


class CopilotScenario(Base, TimestampMixin):
    """一个 Copilot 场景的业务定义（卡片墙 / 工作区 / 提问模板）。"""

    __tablename__ = "copilot_scenario"
    __table_args__ = (
        UniqueConstraint("scenario_code", name="uq_copilot_scenario_code"),
        Index("ix_copilot_scenario_enabled_sort", "enabled", "sort_order"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)

    scenario_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    icon_code: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="robot"
    )
    # 钉死的 Agent（保存时校验存活；运行时 fail loud）
    agent_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # 槽位定义数组（type: number/dateRange/entityMulti/dictMulti/text）
    slot_schema: Mapped[list[dict]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    # 提问模板：{slot} 占位 + 冲突仲裁尾注（后端渲染）
    question_template: Mapped[str] = mapped_column(Text, nullable=False)
    # 预设槽位组合（推荐问题 chips）
    chips: Mapped[list[dict]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    # 结果渲染器（白名单见 RESULT_RENDERER_WHITELIST）
    result_renderer: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="none"
    )
    # 可见角色（NULL=全员；admin=仅管理员）
    required_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="100"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
