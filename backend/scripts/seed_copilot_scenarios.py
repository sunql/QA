"""seed_copilot_scenarios - 幂等 upsert Copilot 场景配置（P4）。

场景从硬编码（ProcurementCopilotPage.tsx）变为 DB 业务资产：卡片墙 /
工作区 / 提问模板 / 结果渲染器全部由 `copilot_scenario` 驱动；加场景 =
追加一个 SEED 并重跑本脚本。

UI 优先策略（与 seed_menu_config 同款）：仅在「行不存在」时 INSERT；
行已存在时**不覆盖任何字段** —— 场景配置是治理资产，改配置走 SQL /
seed 管理（第一期管理口径，2026-11-24 用户确认），seed 不回滚。

冲突让位启发式（conflictKeywords / codePattern）来自 2026-11-24 生产
反馈：纯编码提问（如「分析一下B109待到货情况」）也算指定维度。
codePattern 必须用显式 lookaround 声明边界 —— Python ``\\b`` 在
CJK↔字母数字边界不生效。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import getSettings
from app.domain.copilot_scenario_models import CopilotScenario

# 短编码 = 供应商码（B109/F109）；长编码 = 物料码（AJE0000781 /
# 201000000000001111D，尾缀字母属于编码的一部分）；T3/C1 = 基地码。
_SUPPLIER_CODE = r"(?<![0-9A-Za-z])[A-Z]\d{2,4}(?![0-9A-Za-z])"
_MATERIAL_CODE = r"(?<![0-9A-Za-z])(?:[A-Z]{2,}\d{4,}[A-Z]?|\d{10,}[A-Z]?)(?![0-9A-Za-z])"
_SITE_CODE = r"(?<![0-9A-Za-z])(?:T\d{1,2}|C\d)(?![0-9A-Za-z])"

PENDING_DELIVERY_SLOT_SCHEMA: list[dict[str, Any]] = [
    {
        "key": "monthsBack",
        "type": "number",
        "label": "时间窗",
        "required": True,
        "conflictKeywords": ["时间窗", "个月", "时间段", "日期", "上半年", "下半年", "本年度", "本年"],
        "displayTemplate": "近{value}个月",
    },
    {
        "key": "supplierKeys",
        "type": "entityMulti",
        "label": "供应商",
        "conflictKeywords": ["供应商", "厂家"],
        "codePattern": _SUPPLIER_CODE,
    },
    {
        "key": "materialKeys",
        "type": "entityMulti",
        "label": "物料",
        "conflictKeywords": ["物料", "材料"],
        "codePattern": _MATERIAL_CODE,
    },
    {
        "key": "siteKeys",
        "type": "dictMulti",
        "label": "基地",
        "conflictKeywords": ["基地", "厂区", "工厂", "车间"],
        "codePattern": _SITE_CODE,
    },
    {"key": "minOverdueDays", "type": "number", "label": "最低逾期天数"},
    # admin 数据源选择（治理动作）：仅 admin 前端会传；非 admin 缺省 ⇒ 走默认绑定。
    # hidden = 前端槽位表单不渲染（由数据源下拉写入），后端渲染照常进尾注/入参
    {
        "key": "datasourceId",
        "type": "number",
        "label": "数据源",
        "conflictKeywords": ["数据源"],
        "hidden": True,
    },
    # 显式日期区间（高级参数「时间段」模式）：覆盖 monthsBack 的近 N 月口径
    {
        "key": "dateFrom",
        "type": "text",
        "label": "开始日期",
        "conflictKeywords": ["日期", "时间段"],
        "hidden": True,
    },
    {
        "key": "dateTo",
        "type": "text",
        "label": "结束日期",
        "conflictKeywords": ["日期", "时间段"],
        "hidden": True,
    },
]

SEEDS: list[dict[str, Any]] = [
    {
        "scenario_code": "pending_delivery",
        "name": "待到货 / 逾期未到",
        "description": "按供应商汇总欠交量并给催货建议，引用订单号与物料编码",
        "icon_code": "car",
        "agent_code": "PROCUREMENT_SOURCING",
        "slot_schema": PENDING_DELIVERY_SLOT_SCHEMA,
        "question_template": "分析待到货情况",
        "chips": [
            {"label": "全部供应商概览（近 12 个月）", "monthsBack": 12, "minOverdueDays": 0},
            {"label": "近期欠交（近 3 个月）", "monthsBack": 3, "minOverdueDays": 0},
            {"label": "逾期超 30 天的欠交", "monthsBack": 12, "minOverdueDays": 30},
            {"label": "长期逾期（超 90 天）", "monthsBack": 12, "minOverdueDays": 90},
        ],
        "result_renderer": "pending_delivery",
        "required_role": None,
        "sort_order": 10,
        "enabled": True,
    },
    # P4 阶段4 验收场景：**纯配置加出**（零前端/后端改动）——绑定既有
    # SUPPLIER_RISK_AGENT（supplier_risk 工具吃单个 key）。无结构化结果
    # 渲染器 ⇒ result_renderer=none（答案走线程 Markdown 卡）。
    {
        "scenario_code": "supplier_risk",
        "name": "供应商风险速览",
        "description": "按供应商编码评估交付/质量/财务风险并给应对建议",
        "icon_code": "search",
        "agent_code": "SUPPLIER_RISK_AGENT",
        "slot_schema": [
            {
                "key": "supplierKey",
                "type": "text",
                "label": "供应商（编码/企业键/名称/简称）",
                "conflictKeywords": ["供应商", "厂家"],
                "codePattern": _SUPPLIER_CODE,
            },
        ],
        "question_template": "分析供应商风险情况",
        # 必填槽位不带默认值 ⇒ 不设 chips（chips 空槽位发送会 422）
        "chips": [],
        # 专用结果卡：头部「供应商 编码（企业键 …）+ 名称 + 分析结论」
        "result_renderer": "supplier_risk",
        "required_role": None,
        "sort_order": 20,
        "enabled": True,
    },
    # P5 到货跟踪：与待到货（pending_delivery）不同 —— 关注「已到/未到」的
    # 到货达成（订单到货率 + 三态），按企业 24 日月度周期取窗。
    # 无 datasourceId 槽位（THBI 硬规则：数据源由本体绑定唯一确定）；
    # 无 materialKeys 槽位（编排方裁决：用户需求不含按物料过滤，工具侧亦未实现）。
    {
        "scenario_code": "delivery_tracking",
        "name": "到货跟踪",
        "description": "按周期/供应商跟踪订单到货达成，含已到/未到物料明细",
        "icon_code": "car",
        "agent_code": "DELIVERY_TRACKING_AGENT",
        "slot_schema": [
            # 非必填：工具缺省近 12 个月；给 anchorDay/periodIndex 时按锚点周期取窗，
            # monthsBack 被忽略（窗口优先级：显式区间 > 锚点周期 > monthsBack 默认）。
            # 若设为 required，24 日周期 chips 将无法通过「必填槽位必须在 chip 里有值」的种子 lint。
            {
                "key": "monthsBack",
                "type": "number",
                "label": "时间窗",
                "conflictKeywords": ["时间窗", "个月", "时间段", "日期", "上半年", "下半年", "本年度", "本年"],
                "displayTemplate": "近{value}个月",
            },
            {
                "key": "anchorDay",
                "type": "number",
                "label": "月度周期锚点日",
                "conflictKeywords": ["周期", "24日", "结算周期"],
            },
            {
                "key": "periodIndex",
                "type": "number",
                "label": "周期期序",
                "conflictKeywords": ["本期", "上期", "上个月"],
            },
            {
                "key": "supplierKeys",
                "type": "entityMulti",
                "label": "供应商",
                "conflictKeywords": ["供应商", "厂家"],
                "codePattern": _SUPPLIER_CODE,
            },
            {
                "key": "orderKeys",
                "type": "entityMulti",
                "label": "订单",
                "conflictKeywords": ["订单", "订单号"],
            },
        ],
        "question_template": "分析订单到货情况",
        "chips": [
            {"label": "近 12 个月到货概览", "monthsBack": 12},
            {"label": "近 3 个月到货概览", "monthsBack": 3},
            {"label": "本期到货概览（24 日周期）", "anchorDay": 24, "periodIndex": 0},
            {"label": "上期到货概览（24 日周期）", "anchorDay": 24, "periodIndex": -1},
        ],
        "result_renderer": "delivery_tracking",
        "required_role": None,
        "sort_order": 30,
        "enabled": True,
    },
]


async def seed_copilot_scenarios(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """幂等 upsert 场景配置。返回 seed 总数。

    冲突键：`scenario_code`（全局唯一）。重复运行不新增行、不刷新字段。
    """
    async with session_factory() as session:
        for seed in SEEDS:
            stmt = (
                pg_insert(CopilotScenario)
                .values(**seed)
                .on_conflict_do_nothing(index_elements=["scenario_code"])
            )
            await session.execute(stmt)
        await session.commit()
    return len(SEEDS)


async def main() -> None:
    """CLI 入口：独立引擎跑种子，跑完释放连接池。"""
    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        n = await seed_copilot_scenarios(factory)
        print(f"seed_copilot_scenarios: {n} scenarios ensured")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
