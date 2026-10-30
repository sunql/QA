"""seed_agent_orchestration_menu — 显式播种编排菜单（灰度期手动执行）。

⚠️ 刻意**不**并入 ``seed_menu_config.ITEMS``：
   默认 seed 是「新装库即有」的清单，把编排入口放进去 = 提前上线。
   编排能力默认关闭（``ENABLE_AGENT_ORCHESTRATION=false`` ⇒ 无新路由、无 Worker），
   菜单入口必须与它一起由运维显式打开（跑一次本脚本）。
   灰度流程见 Harness/changes/feat-agent-orchestration/deploy-gray-rollout.md。

UI 优先策略（与 seed_menu_config 一致）：本 seed 只在「行不存在」时 INSERT 默认值；
行已存在时**不覆盖任何字段**（label_key / icon_code / path / visible / sort_order /
parent_id），让 AdminMenusPage 的用户编辑持久化。

用法：
    cd backend && python -m scripts.seed_agent_orchestration_menu
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

#: 编排菜单挂在「智能体」一级类下（与现有 item.chat / item.agents 同级）。
SECTION_CODE = "section.aiAgent"

#: 两个新菜单项。sort_order 140/150 落在 section.aiAgent 现有子项
#: （110/120/130）之后，不与任何既有项冲突。
ITEMS: list[dict[str, Any]] = [
    {
        "code": "item.agentOrchestration",
        "label_key": "menu.item.agentOrchestration",
        "icon_code": "appstore",
        "sort_order": 140,
        "path": "/agent-orchestrations",
    },
    {
        "code": "item.agentTasks",
        "label_key": "menu.item.agentTasks",
        "icon_code": "thunderbolt",
        "sort_order": 150,
        "path": "/agent-tasks",
    },
]


async def seed_agent_orchestration_menu(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """幂等插入两个编排菜单项。返回**本次实际插入**的行数（首次 2，之后 0）。

    冲突键：`code`（全局唯一）。重复运行不新增行，也不刷新任何字段。
    父菜单 ``section.aiAgent`` 不存在时直接报错（fail fast）：静默跳过会让
    「菜单没挂上」变成线上才发现的问题。
    """
    async with session_factory() as session:
        parentId = (
            await session.execute(
                select(MenuConfig.id).where(MenuConfig.code == SECTION_CODE)
            )
        ).scalar_one_or_none()
        if parentId is None:
            raise RuntimeError(
                f"父菜单 {SECTION_CODE} 不存在：请先执行 seed_menu_config，再执行本脚本"
            )

        inserted = 0
        for item in ITEMS:
            stmt = (
                pg_insert(MenuConfig)
                .values(
                    code=item["code"],
                    parent_id=parentId,
                    label_key=item["label_key"],
                    icon_code=item["icon_code"],
                    sort_order=item["sort_order"],
                    path=item["path"],
                    visible=True,
                )
                .on_conflict_do_nothing(index_elements=["code"])
                # RETURNING 只回传真正插入的行 ⇒ 返回值即「本次插入数」
                .returning(MenuConfig.id)
            )
            result = await session.execute(stmt)
            inserted += len(result.all())

        await session.commit()
    return inserted


async def main() -> None:
    """CLI 入口：独立引擎跑种子，跑完释放连接池。"""
    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        n = await seed_agent_orchestration_menu(factory)
        print(f"seed_agent_orchestration_menu: {n} rows inserted")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
