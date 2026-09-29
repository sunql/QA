"""收敛两库结构漂移：补 menu_config 可见性索引 + 把 users 遗留列写进迁移。

2026-09-12 逐列/逐索引比对 `qa_metadata`（prod）与 `qa_metadata_test` 时发现
**双向漂移** —— 两库 `alembic_version` 都是 ``0059_wiki_coverage_tables``，
但结构并不相同。根因是 Alembic **只记录「迁移脚本执行到哪一版」，不校验实际
DDL 是否等于该版应有的样子**：prod 是 dump 恢复出来的，`alembic_version`
一进去就是高位，`upgrade head` 看到「已在 head」直接跳过，早年迁移里建的
对象永远不会被补上；反之手工 DDL 也不会被任何迁移接管。

本迁移处理两处：

1. **prod 缺 ``ix_menu_config_visible_sort``**。它由 0033 创建、``app/models/
   menu_config.py`` 的 ``MenuConfig.__table_args__`` 也一直声明着，唯独 prod
   没有。纯补建，无数据风险。

2. **prod `users` 多 4 列 + 1 个部分索引**，来自 ``docs/superpowers/plans/
   2026-09-08-user-auth.md`` —— 该计划描述的密码登录**从未落地**（``app/models/
   rbac.py`` 的 ``User`` 至今无密码字段），这 4 列是当时手工 DDL 的残留，
   任何迁移与模型都不认识它们。这里把它们**写进迁移正式建档**，让「新建库」
   与「prod」结构一致，不再有只存在于某个库里的列。

**为什么不删这 4 列**：prod 上唯一的那个 admin 用户 ``password_hash`` 非空、
``must_change_password`` 为 true —— 是真实凭据材料，不是空列。删除等于丢数据，
必须由人显式决定，不能顺手做。

**为什么 downgrade 只删索引、不删列**（刻意不对称）：prod 上这些列**早于本
迁移存在**且带真实数据，`alembic downgrade` 不该成为一条隐形的数据销毁路径。
真要清理这 4 列，请走一次有备份、经确认的独立迁移。

幂等守卫 ``IF NOT EXISTS`` 与 0053-0059 同模式：prod 上对象已存在，重复执行
必须是 no-op 而不是报错。

Revision ID: 0060
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0060_schema_reconcile"
down_revision: str | None = "0059_wiki_coverage_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# users 上 4 个遗留列的 DDL 片段：与 prod 实际形状逐字对齐（类型/可空/默认值），
# 否则「新建库」与 prod 只是列名相同、行为不同，漂移会以更隐蔽的形式留下。
_USERS_LEGACY_COLUMN_DDL = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password "
    "BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at "
    "TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_ip VARCHAR(45)",
)


def upgrade() -> None:
    for ddl in _USERS_LEGACY_COLUMN_DDL:
        op.execute(ddl)

    # 部分索引：全表绝大多数行 must_change_password=false，谓词索引把「待改密用户」
    # 的查询压到极小的索引上，与「给 bool 列加全表索引」是不同的东西。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_users_must_change_password
        ON users (must_change_password)
        WHERE must_change_password = true
        """
    )

    # prod 缺失的那一个（0033 已创建、ORM 已声明）。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_menu_config_visible_sort
        ON menu_config (visible, sort_order)
        """
    )


def downgrade() -> None:
    # 只回退本迁移新建的索引。users 的 4 个遗留列**刻意不删**：它们在 prod 上
    # 早于本迁移存在且带真实数据，见模块 docstring。
    op.execute("DROP INDEX IF EXISTS ix_menu_config_visible_sort")
    op.execute("DROP INDEX IF EXISTS ix_users_must_change_password")
