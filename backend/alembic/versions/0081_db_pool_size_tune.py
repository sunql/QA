"""把 ``DB_POOL_SIZE`` 系统参数从旧默认 ``5`` 提升到 ``20``（feat-chat-concurrency follow-up 1）。

**触发**：50 并发场景下 ``pool_size=5, max_overflow=10`` 总连接数 15 不足，
35 个请求需排队等池，造成 latency spike。本迁移只把仍是旧默认 ``5`` 的行升级到
``20``，保留任何手动调过的值（admin-PUT 设置的 ``30``/``50`` 等不动）。

**与 docker/docker-compose.yml 的协同**：本次同步把 ``postgres`` 服务的
``command`` 加上 ``max_connections=200``，给 30 个元库连接 + 其它临时连接
（alembic migration、admin 页面）留余量。重启 PG 容器生效，**数据保留**。

**幂等性**：
- ``upgrade`` UPDATE 带 WHERE ``value='5'``——已升过或被手动改过的行不动
- ``downgrade`` 对称：UPDATE WHERE ``value='20'`` → ``5``（仅在值正是 20 时回退，
  避免误伤手工调整）

**Settings 默认同步**：``app/config.py`` 的 ``dbPoolSize`` 默认值同步从 5 改为 20，
这样即使没 seed 行（如本迁移之前有人 DELETE 过 DB_POOL_SIZE），
启动 bootstrap 走 Settings 默认也能拿到 20。

**两库同步**：升级到 head；prod 与 qa_metadata_test 都需要。

Revision ID: 0081
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision: str = "0081_db_pool_size_tune"
down_revision: str | None = "0080_llm_concurrency_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 仅当行还是旧默认 5 时才升级，避免覆盖任何手动调整。
    # 不会触发 alembic schema 变化（仅数据），所以迁移幂等。
    # 同时把 description 的「默认: 5」字面量刷新到 20，让 admin 页面显示一致。
    op.execute(
        text(
            "UPDATE system_config "
            "SET value = '20', "
            "    description = replace(description, '默认: 5', '默认: 20'), "
            "    updated_time = now() "
            "WHERE key = 'DB_POOL_SIZE' AND value = '5'"
        )
    )


def downgrade() -> None:
    # 对称回退：仅当值正是 20 时回退到 5。
    # 与 0079 同样保守：不动任何手工调整过的值。
    op.execute(
        text(
            "UPDATE system_config "
            "SET value = '5', "
            "    description = replace(description, '默认: 20', '默认: 5'), "
            "    updated_time = now() "
            "WHERE key = 'DB_POOL_SIZE' AND value = '20'"
        )
    )