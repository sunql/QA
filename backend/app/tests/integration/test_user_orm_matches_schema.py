"""`users` 表的 ORM 声明必须与真实 DB 结构逐列一致。

**为什么单独钉死这张表**：2026-09-12 发现 prod `users` 多出 4 列
（`password_hash` / `must_change_password` / `last_login_at` / `last_login_ip`）
+ 一个部分索引，来自 `docs/superpowers/plans/2026-09-08-user-auth.md` 那次
**从未落地的**密码登录改造 —— 手工 DDL 残留，任何迁移与模型都不认识它们。
后果不只是「多几列」：没人知道它们从哪来、能不能删、删了会丢什么
（prod 唯一 admin 的 `password_hash` 是非空 bcrypt，删了就真丢）。

Alembic 0060 把列写进了迁移，本文件把列**钉进 ORM**，两侧合起来才算建档完成：
迁移负责「新建库也有这些列」，ORM 负责「`alembic autogenerate` 不会把它们
当成多余对象 DROP 掉」。只做前者，autogenerate 每次都会提议删列。

最后一条用例是安全不变量：`password_hash` 进了 ORM 之后，
必须保证它**不会**顺着任何 `*Read` DTO 漏出去。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User
from app.schemas.rbac import UserRead

_LEGACY_COLUMNS = (
    "password_hash",
    "must_change_password",
    "last_login_at",
    "last_login_ip",
)


async def _dbColumns(dbSession: AsyncSession, table: str) -> set[str]:
    result = await dbSession.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {row[0] for row in result}


@pytest.mark.asyncio
async def test_orm_columns_match_db_columns(dbSession: AsyncSession) -> None:
    """列集合必须双向相等 —— 这次漂移的根因检查。

    比「ORM 少列」（autogenerate 会 DROP 掉真实数据）和「ORM 多列」
    （迁移漏了，新建库缺列）都危险，方向相反但都由同一条断言拦住。
    """
    ormColumns = set(User.__table__.columns.keys())
    dbColumns = await _dbColumns(dbSession, "users")

    onlyInOrm = ormColumns - dbColumns
    onlyInDb = dbColumns - ormColumns
    assert ormColumns == dbColumns, (
        f"users 表 ORM 与 DB 列不一致：仅 ORM 有 {sorted(onlyInOrm)}，"
        f"仅 DB 有 {sorted(onlyInDb)}"
    )


def test_legacy_columns_are_declared_on_orm() -> None:
    """4 个遗留列必须显式声明在 ORM 上（否则 autogenerate 会提议 DROP）。"""
    columns = User.__table__.columns

    for name in _LEGACY_COLUMNS:
        assert name in columns, (
            f"users.{name} 未在 ORM 声明；alembic autogenerate 会把它当成"
            "多余列并生成 DROP，而 prod 上这列有真实数据"
        )

    assert columns["password_hash"].type.length == 255
    assert columns["password_hash"].nullable is True
    assert columns["must_change_password"].nullable is False
    assert columns["last_login_ip"].type.length == 45
    assert columns["last_login_at"].nullable is True


def test_partial_index_is_declared_with_predicate() -> None:
    """部分索引要连同 WHERE 谓词一起声明 —— 丢了谓词就成了全表索引，是另一回事。"""
    indexes = {idx.name: idx for idx in User.__table__.indexes}

    assert "ix_users_must_change_password" in indexes, (
        "ORM 未声明 ix_users_must_change_password"
    )
    whereClause = indexes["ix_users_must_change_password"].dialect_options[
        "postgresql"
    ].get("where")
    assert whereClause is not None, "部分索引缺少 postgresql_where 谓词"
    assert "must_change_password" in str(whereClause)


def test_password_hash_never_exposed_in_read_dto() -> None:
    """安全不变量：凭据哈希不得出现在任何对外 DTO 里。

    本仓库的 DTO 是显式字段声明（`UserRead` 不继承 ORM），所以这条目前是
    定义性成立的 —— 钉它是因为「把列加进 ORM」这一步很自然会被顺手写成
    `from_attributes` 全字段透出，那才是泄漏时刻。
    """
    assert "password_hash" not in UserRead.model_fields
    assert "password_hash" not in UserRead.model_json_schema()["properties"]
