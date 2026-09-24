"""seed_menu_config - 幂等 upsert 7 类 35 项菜单（共 42 行）。

与 AppLayout 的旧 key 一一对应；feat-rbac-identity 追加 4 个 RBAC 管理页
（用户/角色/组织/菜单）叶子项；feat-wiki-knowledge 追加 1 个一级类
「企业 Wiki」+ 5 个二级项（知识条目/导入/冲突/建议/覆盖度）。

UI 优先策略：本 seed 只在「行不存在」时 INSERT 默认值；行已存在时
**不覆盖任何 UI 可编辑字段**（label_key / icon_code / path / visible /
sort_order / parent_id），让 AdminMenusPage 的用户编辑持久化。
- 字段语义：seed 提供「初始值 / SSOT 起点」，DB 是「运行时真值」。
- 新加菜单项：把 ITEMS / SECTIONS 追加一行；INSERT 时写入全套 seed 默认。
- 改菜单文案 / 图标 / URL：去 /admin/menus 改，seed 不回滚。
- 改 sort_order / 拖动改父级：去 /admin/menus 改，seed 不回滚。
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
    # feat-wiki-knowledge：知识管理自成一级（240 是本段唯一空闲的百位区间，
    # 放在「智能分析」之后、「业务配置」之前，不至于被排到侧边栏最底部）
    {"code": "section.enterpriseWiki", "label_key": "menu.section.enterpriseWiki", "icon_code": "book", "sort_order": 240},
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
    # 企业 Wiki（feat-wiki-knowledge）：二级项 = 机制 1-6 各自的落地面板。
    # 「知识导入」原挂在 section.systemConfig（590），本次归位到本段的 260 ——
    # 它一直是 wiki 的功能，只是先有了页面、后有了分组。
    # feat-wiki-chat：Wiki Chat 对话入口放组内第一位（245 < 250）——
    # 知识消费（对话问答）是使用者的第一入口，管理面板跟在后面。
    {"parent": "section.enterpriseWiki", "code": "item.wikiChat", "label_key": "menu.item.wikiChat", "icon_code": "message", "sort_order": 245, "path": "/wiki-chat"},
    {"parent": "section.enterpriseWiki", "code": "item.wikiPages", "label_key": "menu.item.wikiPages", "icon_code": "file", "sort_order": 250, "path": "/admin/wiki-pages"},
    {"parent": "section.enterpriseWiki", "code": "item.wikiImport", "label_key": "menu.item.wikiImport", "icon_code": "import", "sort_order": 260, "path": "/admin/wiki-import"},
    {"parent": "section.enterpriseWiki", "code": "item.wikiConflicts", "label_key": "menu.item.wikiConflicts", "icon_code": "alert", "sort_order": 270, "path": "/admin/wiki-conflicts"},
    {"parent": "section.enterpriseWiki", "code": "item.wikiSuggestions", "label_key": "menu.item.wikiSuggestions", "icon_code": "tool", "sort_order": 280, "path": "/admin/wiki-suggestions"},
    {"parent": "section.enterpriseWiki", "code": "item.wikiCoverage", "label_key": "menu.item.wikiCoverage", "icon_code": "dashboard", "sort_order": 290, "path": "/admin/wiki-coverage"},
    # Phase 2 知识图谱：Louvain 社区 + 4-Signal 相关性可视化
    {"parent": "section.enterpriseWiki", "code": "item.wikiGraph", "label_key": "menu.item.wikiGraph", "icon_code": "node", "sort_order": 295, "path": "/admin/wiki-graph"},
    # Business Config
    {"parent": "section.bizConfig", "code": "item.ontology", "label_key": "menu.item.ontology", "icon_code": "partition", "sort_order": 310, "path": "/ontology"},
    {"parent": "section.bizConfig", "code": "item.dataQuality", "label_key": "menu.item.dataQuality", "icon_code": "audit", "sort_order": 320, "path": "/data-quality"},
    # feat-dq-rule-params Task 11
    {"parent": "section.bizConfig", "code": "item.dataQualityRuleParams", "label_key": "menu.item.dataQualityRuleParams", "icon_code": "audit", "sort_order": 325, "path": "/data-quality/rule-params"},
    # feat-dq-evaluation-report：评估报告页（路由 /data-quality/reports → 重定向 ?tab=reports
    # 在 DataQualityPage 内的 reports tab；本菜单项仅作直接入口与发现性，不替代 tab 体验）。
    {"parent": "section.bizConfig", "code": "item.dataQualityReport", "label_key": "menu.item.dataQualityReport", "icon_code": "fund", "sort_order": 326, "path": "/data-quality/reports"},
    # item.dataQualityGenerate 已下沉为 DataQualityPage 的 tab（?tab=generate）；
    # 旧路由由 App.tsx 的 <Navigate> 重定向到 ?tab= 参数。此处不再发菜单项。
    {"parent": "section.bizConfig", "code": "item.lineage", "label_key": "menu.item.lineage", "icon_code": "node", "sort_order": 330, "path": "/lineage"},
    {"parent": "section.bizConfig", "code": "item.entityMapping", "label_key": "menu.item.entityMapping", "icon_code": "code", "sort_order": 340, "path": "/entity-mapping"},
    {"parent": "section.bizConfig", "code": "item.kpiCatalog", "label_key": "menu.item.kpiCatalog", "icon_code": "number", "sort_order": 350, "path": "/kpi-catalog"},
    {"parent": "section.bizConfig", "code": "item.features", "label_key": "menu.item.features", "icon_code": "cluster", "sort_order": 360, "path": "/features"},
    {"parent": "section.bizConfig", "code": "item.adminFeatureRules", "label_key": "menu.item.adminFeatureRules", "icon_code": "tool", "sort_order": 365, "path": "/admin/feature-rules"},
    {"parent": "section.bizConfig", "code": "item.businessObjects", "label_key": "menu.item.businessObjects", "icon_code": "cluster", "sort_order": 370, "path": "/business-objects"},
    {"parent": "section.bizConfig", "code": "item.ontologyProperties", "label_key": "menu.item.ontologyProperties", "icon_code": "tags", "sort_order": 375, "path": "/ontology-properties"},
    # Foundation
    {"parent": "section.foundation", "code": "item.datasource", "label_key": "menu.item.datasource", "icon_code": "database", "sort_order": 410, "path": "/datasource"},
    # feat-local-import-evolution：本地数据初始化独立入口页 /local-import（复用 ImportWizard）
    {"parent": "section.foundation", "code": "item.localImport", "label_key": "menu.item.localImport", "icon_code": "import", "sort_order": 415, "path": "/local-import"},
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
    {"parent": "section.systemConfig", "code": "item.adminSystemConfig", "label_key": "menu.item.adminSystemConfig", "icon_code": "setting", "sort_order": 580, "path": "/admin/system-config"},
    # Audit & Security（仅保留审计日志）
    {"parent": "section.auditSecurity", "code": "item.adminAudit", "label_key": "menu.item.adminAudit", "icon_code": "audit", "sort_order": 610, "path": "/admin/audit"},
    # item.adminFeatureRules 已搬到 section.bizConfig sort_order=365（与 item.features 配套）
]


async def seed_menu_config(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """幂等 upsert 7 个一级类 + 35 个叶子项。返回总行数（42）。

    冲突键：`code`（全局唯一）。重复运行不新增行，也不刷新任何字段 —— 已存在的行
    完全交给菜单管理 UI 维护（见文件头「UI 优先策略」）。
    """
    async with session_factory() as session:
        # 1) upsert sections：仅在「行不存在」时写入 seed 默认；冲突时 DO NOTHING
        #    —— 所有 UI 可编辑字段（label_key / icon_code / sort_order / visible）
        #    一律由 AdminMenusPage 维护，seed 不回滚。
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
                .on_conflict_do_nothing(index_elements=["code"])
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

        # 3) upsert items：仅 INSERT；冲突时 DO NOTHING，让 UI 编辑（label_key /
        #    icon_code / path / visible / sort_order / parent_id）持久化。
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
                .on_conflict_do_nothing(index_elements=["code"])
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
