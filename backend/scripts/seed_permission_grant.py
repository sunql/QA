"""seed_permission_grant - 幂等 upsert 4 个 demo 组织（采购/质量/财务/数据）的菜单权限。

背景：feat-rbac-identity 给 admin 用户走 admin 角色旁路（不过滤），但
非 admin DB 用户走 `PermissionService.computeEffective`，其三维度
（direct ∪ role ∪ organization）合集决定可见菜单。本特性之前只有
管理员组织（org_id=1）seed 了 36 行 grant，4 个 demo 组织（id=2-5）
完全 0 grant → demo 用户登录后 /menu-config 返回空 → 前端走
FALLBACK_NAV（扁平、无分组）。

修法：按业务给 4 个 demo 组织各 seed 一组菜单码，与 menu_config SSOT
对齐（seed_menu_config.py 的 ITEMS 集合的子集）。

幂等约定：on_conflict_do_update + 空 set（subject_type + subject_id +
menu_code 是 UQ），不吞行；不覆盖已有 granted_at / id。

菜单子集设计（按业务最小可工作集 + 全员 chat / profile）：
  - 采购部门（id=2）：供应商、数据源、本体、AIChat、状态、监控
  - 质量部门（id=3）：数据质量（含 reports / rule-params）、AIChat、本体、状态
  - 财务部门（id=4）：指标、用量、特性、AIChat、状态
  - 数据团队（id=5）：血缘、实体映射、图、向量、本体、AIChat、状态、文档

每个组织都含 ``item.chat`` + ``item.profile`` + ``item.changePassword`` —
chat 是 AI 入口，profile/changePassword 是个人面板（无业务依赖）。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import getSettings
from app.models.rbac import PermissionGrant

logger = logging.getLogger(__name__)

# (org_id, [menu_code, ...]) —— 与 menu_config SSOT 对齐（seed_menu_config.py ITEMS）。
_ORG_GRANTS: list[tuple[int, list[str]]] = [
    (
        2,  # 采购部门
        [
            "item.chat",
            "item.profile",
            "item.changePassword",
            "item.supplier360",
            "item.supplierRisk",
            "item.datasource",
            "item.ontology",
            "item.ontologyProperties",
            "item.models",  # 只读，看配置
            "item.status",
        ],
    ),
    (
        3,  # 质量部门
        [
            "item.chat",
            "item.profile",
            "item.changePassword",
            "item.dataQuality",
            "item.dataQualityReport",
            "item.dataQualityRuleParams",
            "item.ontology",
            "item.status",
        ],
    ),
    (
        4,  # 财务部门
        [
            "item.chat",
            "item.profile",
            "item.changePassword",
            "item.kpiCatalog",
            "item.usage",
            "item.features",
            "item.models",  # 只读，看用量
            "item.status",
        ],
    ),
    (
        5,  # 数据团队
        [
            "item.chat",
            "item.profile",
            "item.changePassword",
            "item.lineage",
            "item.entityMapping",
            "item.graph",
            "item.vectors",
            "item.documents",
            "item.ontology",
            "item.datasource",
            "item.localImport",
            "item.status",
        ],
    ),
]


async def seed_permission_grant(factory: async_sessionmaker[AsyncSession]) -> int:
    """幂等 upsert 4 个 demo 组织的菜单权限。

    返回 upsert 的行数（不含已存在且 set 为空的命中）。
    """
    async with factory() as session:
        total = 0
        for org_id, codes in _ORG_GRANTS:
            for menu_code in codes:
                stmt = (
                    pg_insert(PermissionGrant)
                    .values(
                        subject_type="ORGANIZATION",
                        subject_id=org_id,
                        menu_code=menu_code,
                    )
                    .on_conflict_do_update(
                        index_elements=["menu_code", "subject_type", "subject_id"],
                        # 幂等不覆盖任何字段 —— self-update 赋同值避开
                        # SQLAlchemy "set must not be empty" 校验；行内容
                        # 不变但 PG 视作 UPDATE（rowcount=0 时仅命中已存在行）。
                        set_={"menu_code": pg_insert(PermissionGrant).excluded.menu_code},
                    )
                )
                result = await session.execute(stmt)
                # PG 行数 > 0 才是真 upsert 写入；set_={} 时 0 表示命中
                if result.rowcount and result.rowcount > 0:
                    total += 1
        await session.commit()
        return total


async def main() -> None:
    """CLI 入口：独立引擎跑种子，跑完释放连接池。"""
    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        n = await seed_permission_grant(factory)
        logger.info("seed_permission_grant: %d new rows upserted (existing rows kept)", n)
        print(f"seed_permission_grant: {n} new rows upserted")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())