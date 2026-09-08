"""seed_menu_config - 幂等 upsert 6 类 28 项菜单。

与 AppLayout 的旧 key 一一对应；feat-rbac-identity 追加 4 个 RBAC 管理页
（用户/角色/组织/菜单）叶子项，零 section 变更（section 数保持 6，测试断言依赖）。

parent_id 保留策略：seed 仅在「当前 DB 行 parent_id 与 seed 默认一致」
（即未被菜单管理 UI 移动过）时覆盖；UI 调整后的 parent_id 不被回滚。
其他字段（label_key / icon_code / sort_order / path / visible）每次启动
仍按 seed 默认强制刷新——这些是「代码态事实」，不应被 UI 覆盖。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import getSettings
from app.models.menu_config import MenuConfig

# sort_order 间隔 10，便于插入新项。
SECTIONS: list[dict[str, Any]] = [
    {"code": "section.aiAgent", "label_key": "menu.section.aiAgent", "icon_code": "robot", "sort_order": 100},
    {"code": "section.analytics", "label_key": "menu.section.analytics", "icon_code": "fund", "sort_order": 200},
    {"code": "section.bizConfig", "label_key": "menu.section.bizConfig", "icon_code": "setting", "sort_order": 300},
    {"code": "section.foundation", "label_key": "menu.section.foundation", "icon_code": "database", "sort_order": 400},
    {"code": "section.systemConfig", "label_key": "menu.section.systemConfig", "icon_code": "api", "sort_order": 500},
    {"code": "section.auditSecurity", "label_key": "menu.section.auditSecurity", "icon_code": "safety", "sort_order": 600},
]

ITEMS: list[dict[str, Any]] = [
    # AI Agent
    {"parent": "section.aiAgent", "code": "item.chat", "label_key": "menu.item.chat", "icon_code": "message", "sort_order": 110, "path": "/chat"},
    {"parent": "section.aiAgent", "code": "item.agentRuntime", "label_key": "menu.item.agentRuntime", "icon_code": "thunderbolt", "sort_order": 120, "path": "/agents/run"},
    {"parent": "section.aiAgent", "code": "item.agents", "label_key": "menu.item.agents", "icon_code": "appstore", "sort_order": 130, "path": "/agents"},
    # Smart Analytics
    {"parent": "section.analytics", "code": "item.supplier360", "label_key": "menu.item.supplier360", "icon_code": "barchart", "sort_order": 210, "path": "/supplier-360"},
    {"parent": "section.analytics", "code": "item.supplierRisk", "label_key": "menu.item.supplierRisk", "icon_code": "alert", "sort_order": 220, "path": "/supplier-risk"},
    # Business Config
    {"parent": "section.bizConfig", "code": "item.ontology", "label_key": "menu.item.ontology", "icon_code": "partition", "sort_order": 310, "path": "/ontology"},
    {"parent": "section.bizConfig", "code": "item.dataQuality", "label_key": "menu.item.dataQuality", "icon_code": "audit", "sort_order": 320, "path": "/data-quality"},
    {"parent": "section.bizConfig", "code": "item.dataQualityGenerate", "label_key": "menu.item.dataQualityGenerate", "icon_code": "thunderbolt", "sort_order": 325, "path": "/data-quality/generate"},
    {"parent": "section.bizConfig", "code": "item.lineage", "label_key": "menu.item.lineage", "icon_code": "node", "sort_order": 330, "path": "/lineage"},
    {"parent": "section.bizConfig", "code": "item.entityMapping", "label_key": "menu.item.entityMapping", "icon_code": "code", "sort_order": 340, "path": "/entity-mapping"},
    {"parent": "section.bizConfig", "code": "item.kpiCatalog", "label_key": "menu.item.kpiCatalog", "icon_code": "number", "sort_order": 350, "path": "/kpi-catalog"},
    {"parent": "section.bizConfig", "code": "item.features", "label_key": "menu.item.features", "icon_code": "cluster", "sort_order": 360, "path": "/features"},
    {"parent": "section.bizConfig", "code": "item.adminFeatureRules", "label_key": "menu.item.adminFeatureRules", "icon_code": "tool", "sort_order": 365, "path": "/admin/feature-rules"},
    {"parent": "section.bizConfig", "code": "item.businessObjects", "label_key": "menu.item.businessObjects", "icon_code": "cluster", "sort_order": 370, "path": "/business-objects"},
    {"parent": "section.bizConfig", "code": "item.ontologyProperties", "label_key": "menu.item.ontologyProperties", "icon_code": "tags", "sort_order": 375, "path": "/ontology-properties"},
    # Foundation
    {"parent": "section.foundation", "code": "item.datasource", "label_key": "menu.item.datasource", "icon_code": "database", "sort_order": 410, "path": "/datasource"},
    {"parent": "section.foundation", "code": "item.documents", "label_key": "menu.item.documents", "icon_code": "file", "sort_order": 420, "path": "/documents"},
    {"parent": "section.foundation", "code": "item.usage", "label_key": "menu.item.usage", "icon_code": "dashboard", "sort_order": 430, "path": "/usage"},
    {"parent": "section.foundation", "code": "item.graph", "label_key": "menu.item.graph", "icon_code": "apartment", "sort_order": 440, "path": "/graph"},
    {"parent": "section.foundation", "code": "item.vectors", "label_key": "menu.item.vectors", "icon_code": "heart", "sort_order": 450, "path": "/vectors"},
    # AuditSecurity 下不再有 adminFeatureRules（已搬到 bizConfig 365）
    # System Config（feat-rbac-identity：RBAC 管理 4 页归位 → 用户/角色/组织/菜单）
    {"parent": "section.systemConfig", "code": "item.models", "label_key": "menu.item.models", "icon_code": "api", "sort_order": 510, "path": "/models"},
    {"parent": "section.systemConfig", "code": "item.embeddings", "label_key": "menu.item.embeddings", "icon_code": "node", "sort_order": 520, "path": "/embeddings"},
    {"parent": "section.systemConfig", "code": "item.status", "label_key": "menu.item.status", "icon_code": "heart", "sort_order": 530, "path": "/status"},
    {"parent": "section.systemConfig", "code": "item.adminUsers", "label_key": "menu.item.adminUsers", "icon_code": "user", "sort_order": 540, "path": "/admin/users"},
    {"parent": "section.systemConfig", "code": "item.adminRoles", "label_key": "menu.item.adminRoles", "icon_code": "team", "sort_order": 550, "path": "/admin/roles"},
    {"parent": "section.systemConfig", "code": "item.adminOrganizations", "label_key": "menu.item.adminOrganizations", "icon_code": "org", "sort_order": 560, "path": "/admin/organizations"},
    {"parent": "section.systemConfig", "code": "item.adminMenus", "label_key": "menu.item.adminMenus", "icon_code": "menu", "sort_order": 570, "path": "/admin/menus"},
    # Audit & Security（仅保留审计日志）
    {"parent": "section.auditSecurity", "code": "item.adminAudit", "label_key": "menu.item.adminAudit", "icon_code": "audit", "sort_order": 610, "path": "/admin/audit"},
    # item.adminFeatureRules 已搬到 section.bizConfig sort_order=365（与 item.features 配套）
]


async def seed_menu_config(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """幂等 upsert 6 个一级类 + 28 个叶子项。返回总行数。

    冲突键：`code`（全局唯一）。重复运行不新增行，仅刷新 label_key / icon_code /
    sort_order / path / visible。`parent_id` 仅在首次 INSERT 时设置；已存在的
    行其 parent_id 由菜单管理 UI 维护，seed 不覆盖。
    """
    async with session_factory() as session:
        # 1) upsert sections
        for s in SECTIONS:
            stmt = (
                pg_insert(MenuConfig)
                .values(
                    code=s["code"],
                    parent_id=None,
                    label_key=s["label_key"],
                    icon_code=s["icon_code"],
                    sort_order=s["sort_order"],
                    path=None,
                    visible=True,
                )
                .on_conflict_do_update(
                    index_elements=["code"],
                    set_={
                        "label_key": s["label_key"],
                        "icon_code": s["icon_code"],
                        "sort_order": s["sort_order"],
                        "visible": True,
                    },
                )
            )
            await session.execute(stmt)

        # 2) resolve parent_id by code（一次性查所有一级类的 id）
        section_codes = [s["code"] for s in SECTIONS]
        rows = (
            await session.execute(
                select(MenuConfig.code, MenuConfig.id).where(
                    MenuConfig.code.in_(section_codes)
                )
            )
        ).all()
        code_to_id = {code: rid for code, rid in rows}

        # 3) upsert items：parent_id 仅在首次插入时设置；后续仅刷新展示字段。
        # 理由：菜单管理 UI 允许把叶子项从某个一级类拖到另一个；这种「用户态
        # 决策」必须被 seed 尊重，否则每次启动都把 adminUsers/Roles/Orgs/Menus
        # 回滚回 auditSecurity。代码态事实（label/icon/sort/path/visible）由
        # seed 持续刷新，因为它们属于「代码 ↔ UI 键映射」的 SSOT。
        for it in ITEMS:
            parent_id = code_to_id[it["parent"]]
            stmt = (
                pg_insert(MenuConfig)
                .values(
                    code=it["code"],
                    parent_id=parent_id,
                    label_key=it["label_key"],
                    icon_code=it["icon_code"],
                    sort_order=it["sort_order"],
                    path=it["path"],
                    visible=True,
                )
                .on_conflict_do_update(
                    index_elements=["code"],
                    set_={
                        "label_key": it["label_key"],
                        "icon_code": it["icon_code"],
                        "sort_order": it["sort_order"],
                        "path": it["path"],
                        "visible": True,
                    },
                )
            )
            await session.execute(stmt)

        await session.commit()
    return len(SECTIONS) + len(ITEMS)


async def main() -> None:
    """CLI 入口：独立引擎跑种子，跑完释放连接池。"""
    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        n = await seed_menu_config(factory)
        print(f"seed_menu_config: {n} rows upserted")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
