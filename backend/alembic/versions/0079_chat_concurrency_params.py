"""seed Chat 并发相关 3 个 system_config key（feat-chat-concurrency-params）。

**背景**：审计发现 ``ChatService`` 单 worker 架构本身写得对（全程 async、
per-request AsyncSession、L4 循环有 max_iterations 兜底），50 人并发会降级
根因是 3 个硬编码参数。本迁移把这 3 个参数迁入 system_config 表，让管理员
通过 /admin/system-config 在线调整，无需改代码：

- ``DB_POOL_SIZE`` / ``DB_MAX_OVERFLOW`` —— ``database.py`` 创建 PG 异步引擎
  时使用的 ``pool_size`` / ``max_overflow``。原来硬编码 5/10，50 并发必排队。
  **生效方式：重启容器**（engine pool 在 startup 一次性定死，PG 不支持 runtime resize）。
- ``RATE_LIMIT_KEY_STRATEGY`` —— 限流维度。默认 ip 走 ``get_remote_address``，
  50 人同一 NAT 一刀切 30/minute。改为 user_id 后按用户隔离。
  **生效方式：立即**（rate_limit.py 模块级 TTL 缓存 + admin PUT 主动 invalidate）。

**Description 字段约定**：本项目 system_config description 已用「可调范围说明」
（0078 起），按 `含义 | 生效方式 | 合法值 | 默认值` 分段，单行 plain text，
admin 页面直接展示。

**幂等**：``ON CONFLICT (key) DO NOTHING``，允许多次 upgrade；prod 已存在行不动。

**两库同步**：升级到 head；prod 与 qa_metadata_test 都需要。

Revision ID: 0079
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision: str = "0079_chat_concurrency_params"
down_revision: str | None = "0078_system_config_class_filter"
branch_labels = None
depends_on = None


_SEED_SQL = text(
    """
    INSERT INTO system_config (key, value, description, updated_time) VALUES
      (
        'DB_POOL_SIZE', '5',
        'PG 异步连接池大小（database.py engine 创建时使用）。'
        '需重启容器生效。合法值: 正整数 | 默认: 5',
        now()
      ),
      (
        'DB_MAX_OVERFLOW', '10',
        'PG 异步连接池 max_overflow（pool 满后允许额外创建的连接数）。'
        '需重启容器生效。合法值: 非负整数 | 默认: 10',
        now()
      ),
      (
        'RATE_LIMIT_KEY_STRATEGY', 'ip',
        '限流 key 策略：ip=按 IP, user_id=按当前用户, ip_user=按 IP+用户组合。'
        '立即生效（admin 改值后下一个请求即用新策略）。'
        '合法值: ip|user_id|ip_user | 默认: ip',
        now()
      )
    ON CONFLICT (key) DO NOTHING
    """
)


def upgrade() -> None:
    op.execute(_SEED_SQL)


def downgrade() -> None:
    # 仅在「未被人修改过」时撤；线上若有手动调整过，留着更安全 —— 严格对称用
    # 条件 DELETE，撤迁移会把行带走（保守路径，0078 同款）。
    op.execute(
        text(
            "DELETE FROM system_config WHERE key IN ("
            "'DB_POOL_SIZE', 'DB_MAX_OVERFLOW', 'RATE_LIMIT_KEY_STRATEGY'"
            ") AND value IN ('5', '10', 'ip')"
        )
    )