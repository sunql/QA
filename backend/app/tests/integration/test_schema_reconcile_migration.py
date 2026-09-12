"""验证 Alembic 0060：menu_config 可见性索引补齐 + users 遗留列正式建档。

**背景（2026-09-12 两库逐列比对发现的双向漂移）**：两个库 `alembic_version`
都是 `0059_wiki_coverage_tables`，但结构并不相同 —— alembic 只记录「迁移跑到
哪一版」，不校验实际 DDL 是否等于该版应有的样子。

- prod `qa_metadata` 缺 `ix_menu_config_visible_sort`（0033 迁移创建，dump 恢复后
  版本号一进去就是高位，`upgrade head` 看到已在 head 直接跳过，索引永远没补上）
- prod `qa_metadata` 的 `users` 多 4 列 + 1 个部分索引，来自 `docs/superpowers/
  plans/2026-09-08-user-auth.md`（密码登录从未落地：`app/models/rbac.py` 的
  `User` 至今无密码字段），属**手工 DDL 残留**，任何迁移/模型都不认识它

0060 把两处对齐：索引补建、遗留列写进迁移成为可复现结构（prod 上那 1 行
admin 已有非空 `password_hash`，**不能**当垃圾列删）。

断言走 `information_schema` / `pg_indexes` 直查真实 PG，不读 ORM 元数据
—— 本迁移的存在意义正是「ORM 元数据与 DB 实际结构不一致」，用 ORM 自证会
把测试写成同义反复。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

# prod `users` 上那 4 个手工残留列的期望形状：(类型, 是否可空, 默认值子串)
_LEGACY_USER_COLUMNS = {
    "password_hash": ("character varying", "YES", None),
    "must_change_password": ("boolean", "NO", "false"),
    "last_login_at": ("timestamp with time zone", "YES", None),
    "last_login_ip": ("character varying", "YES", None),
}


async def _columns(dbSession: AsyncSession, table: str) -> dict[str, tuple]:
    result = await dbSession.execute(
        text(
            "SELECT column_name, data_type, is_nullable, column_default, "
            "       character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {row[0]: row[1:] for row in result}


async def _indexDef(dbSession: AsyncSession, indexName: str) -> str | None:
    result = await dbSession.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = :n"
        ),
        {"n": indexName},
    )
    row = result.first()
    return row[0] if row else None


async def test_users_legacy_columns_are_recorded_in_migration(
    dbSession: AsyncSession,
) -> None:
    """4 个「死列」必须真的存在于 schema（由 0060 建档，不再是手工残留）。"""
    cols = await _columns(dbSession, "users")

    for name, (dataType, nullable, defaultSubstr) in _LEGACY_USER_COLUMNS.items():
        assert name in cols, f"users.{name} 缺失：0060 未把遗留列写进迁移"
        assert cols[name][0] == dataType, f"users.{name} 类型不符"
        assert cols[name][1] == nullable, f"users.{name} 可空性不符"
        if defaultSubstr is not None:
            assert defaultSubstr in (cols[name][2] or ""), (
                f"users.{name} 默认值应含 {defaultSubstr}，实际 {cols[name][2]!r}；"
                "NOT NULL 列没有 server_default 会让历史行的 INSERT 失去兜底"
            )

    assert cols["password_hash"][3] == 255
    assert cols["last_login_ip"][3] == 45


async def test_users_must_change_password_partial_index_exists(
    dbSession: AsyncSession,
) -> None:
    """部分索引：只在 must_change_password=true 的行上建，全表大部分行为 false。"""
    indexDef = await _indexDef(dbSession, "ix_users_must_change_password")

    assert indexDef is not None, "缺少 ix_users_must_change_password"
    assert "must_change_password" in indexDef
    assert "WHERE (must_change_password = true)" in indexDef, (
        f"应保留部分索引谓词，实际：{indexDef}"
    )


async def test_menu_config_visible_sort_index_exists(dbSession: AsyncSession) -> None:
    """prod 缺失的索引（0033 迁移创建，恢复后未补）—— 0060 必须让它就位。"""
    indexDef = await _indexDef(dbSession, "ix_menu_config_visible_sort")

    assert indexDef is not None, "缺少 ix_menu_config_visible_sort"
    assert "menu_config" in indexDef
    assert "visible" in indexDef and "sort_order" in indexDef, (
        f"索引列应为 (visible, sort_order)，实际：{indexDef}"
    )


async def test_migration_is_idempotent_under_reapply(dbSession: AsyncSession) -> None:
    """0060 用 IF NOT EXISTS 守卫：prod 上列/索引已存在，重复执行不得报错。

    这里不复跑 alembic（会改 alembic_version），而是直放一遍迁移里的 DDL，
    证明它在「对象已存在」的库上同样是 no-op。
    """
    for ddl in (
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password "
        "BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at "
        "TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_ip VARCHAR(45)",
        "CREATE INDEX IF NOT EXISTS ix_users_must_change_password "
        "ON users (must_change_password) WHERE must_change_password = true",
        "CREATE INDEX IF NOT EXISTS ix_menu_config_visible_sort "
        "ON menu_config (visible, sort_order)",
    ):
        await dbSession.execute(text(ddl))

    await dbSession.rollback()
