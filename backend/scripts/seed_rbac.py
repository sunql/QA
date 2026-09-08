"""seed_rbac - 幂等 upsert RBAC 基线（admin 超管角色 + admin 用户 + 绑定）。

lifespan 每次启动调用，保证至少有可用的超管入口（admin 角色旁路全量菜单
权限）。code='admin' 角色不可删除（is_builtin 语义在 service 层守卫）。

绑定 user_roles 用 ON CONFLICT DO UPDATE 空 set：幂等但不吞行（见
seed-upsert 约定：不允许 do_nothing）。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Role, User, UserRole
from app.models.rbac import ADMIN_ROLE_CODE

logger = logging.getLogger(__name__)

ADMIN_USER_SEED = {
    "username": "admin",
    "display_name": "系统管理员",
    "email": None,
    "enabled": True,
}


async def seedRbacBaseline(session: AsyncSession) -> int:
    """幂等 upsert admin 角色 + admin 用户 + 绑定关系。返回确保存在的行数(3)。

    内部 commit（对齐其它 seed 函数 seed_feature_rules / seed_agent_tool_configs：
    lifespan 调用方不再单独 commit）。重复运行幂等，无副作用。
    """
    # 1) admin 角色（超管：旁路全量菜单，service 侧不可删/不可移除 admin 用户）
    role_stmt = (
        pg_insert(Role)
        .values(
            code=ADMIN_ROLE_CODE,
            name="系统管理员",
            description="内置超管角色：拥有全部菜单权限（旁路授权校验）。",
        )
        .on_conflict_do_update(
            index_elements=["code"],
            set_={
                "name": "系统管理员",
                "description": "内置超管角色：拥有全部菜单权限（旁路授权校验）。",
            },
        )
    )
    await session.execute(role_stmt)

    # 2) admin 用户
    user_stmt = (
        pg_insert(User)
        .values(
            username=ADMIN_USER_SEED["username"],
            display_name=ADMIN_USER_SEED["display_name"],
            email=ADMIN_USER_SEED["email"],
            enabled=ADMIN_USER_SEED["enabled"],
        )
        .on_conflict_do_update(
            index_elements=["username"],
            set_={
                "display_name": ADMIN_USER_SEED["display_name"],
                "email": ADMIN_USER_SEED["email"],
                "enabled": True,
            },
        )
    )
    await session.execute(user_stmt)

    # 3) 绑定 admin 用户 → admin 角色（先解析 id）
    role_id = (
        await session.execute(
            select(Role.id).where(Role.code == ADMIN_ROLE_CODE)
        )
    ).scalar_one()
    user_id = (
        await session.execute(
            select(User.id).where(User.username == "admin")
        )
    ).scalar_one()
    binding_stmt = (
        pg_insert(UserRole)
        .values(user_id=user_id, role_id=role_id)
        .on_conflict_do_update(
            constraint="user_roles_pkey",
            set_={"user_id": user_id},  # 空操作（幂等保行）
        )
    )
    await session.execute(binding_stmt)
    await session.commit()

    logger.info("seedRbacBaseline: admin role/user ensured")
    return 3


async def main() -> None:
    """Standalone 入口：连真实 PG 跑一次基线 seed（运维 / CI 用）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import getSettings

    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            n = await seedRbacBaseline(session)
            await session.commit()
            print(f"seed_rbac: {n} rows ensured")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
