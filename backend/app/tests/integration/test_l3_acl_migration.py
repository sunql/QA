"""Task 4.1: 验证 l3_acl 表存在 + 插入/查询（audit-fx-4.1）。

l3_acl 表：L3 机密文档访问控制白名单。
- id BIGSERIAL PK
- user_id FK → users.id，index=True
- resource_pattern String(255) — 模糊匹配（dataset:* 等）
- granted_by FK → users.id
- granted_at DateTime server_default=func.now()

TDD：先写测试（RED）→ 实现模型和迁移（GREEN）→ regression。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import L3Acl, User
from app.tests._pg_support import resolveTestDatabaseUrl

_BACKEND_ROOT = None  # not needed: _pg_support._ensureSchema handles alembic internally


@pytest.mark.asyncio
async def test_l3_acl_table_exists(dbSession: AsyncSession) -> None:
    """upgrade head 后：l3_acl 表已创建（to_regclass 非 null）。"""
    url = resolveTestDatabaseUrl()
    # ensureSchema already ran alembic upgrade head; just verify
    result = await dbSession.execute(
        text("SELECT to_regclass('public.l3_acl')")
    )
    assert result.scalar() == "l3_acl"


@pytest.mark.asyncio
async def test_l3_acl_insert_and_query(dbSession: AsyncSession) -> None:
    """L3Acl ORM 可正常插入 + 按 user_id 查询。"""
    # Arrange: find or create a user to satisfy FK
    result = await dbSession.execute(select(User).limit(1))
    user = result.scalars().first()
    if user is None:
        user = User(
            username="l3acl_test_user",
            display_name="L3 ACL Test User",
        )
        dbSession.add(user)
        await dbSession.flush()

    # Also ensure a second user exists for granted_by FK
    result2 = await dbSession.execute(select(User).where(User.id != user.id).limit(1))
    granter = result2.scalars().first()
    if granter is None:
        granter = User(
            username="l3acl_granter",
            display_name="L3 ACL Granter",
        )
        dbSession.add(granter)
        await dbSession.flush()

    # Act: insert via ORM
    await dbSession.execute(
        L3Acl.__table__.insert().values(
            user_id=user.id,
            resource_pattern="dataset:*",
            granted_by=granter.id,
        )
    )
    await dbSession.commit()

    # Assert: query back
    rows = await dbSession.execute(
        select(L3Acl).where(L3Acl.user_id == user.id)
    )
    fetched = rows.scalars().one()
    assert fetched.resource_pattern == "dataset:*"
