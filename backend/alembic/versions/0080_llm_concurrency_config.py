"""seed LLM_CONCURRENCY_LIMIT 到 system_config（feat-chat-concurrency）。

**背景**：审计发现 50 人并发 /chat 时所有请求瞬间全打 LLM provider，触发 429 后
``_callWithFallback`` 立刻重打备选模型，造成 retry storm。step 1 已把 DB pool 与
限流 key 迁入 system_config；本迁移把 LLM 并发上限一并迁入，与 ``asyncio.Semaphore``
绑定，允许 admin 在不重启容器的情况下动态调整全局 LLM 并发上限。

**生效方式**：立即生效。lifespan 启动期一次性读 DB 行注入到
``app.infrastructure.llm.factory.LLMConcurrencyManager``；admin PUT 调
``reload_llm_concurrency_limit`` 创建新 Semaphore（旧的自然跑完，不中断在用请求）。

**与其它并发参数的联动**：
- ``DB_POOL_SIZE`` 控制 metadata DB 连接池（启动期定死，需重启）；
- ``RATE_LIMIT_KEY_STRATEGY`` 控制 IP/user_id 限流维度（立即生效）；
- ``LLM_CONCURRENCY_LIMIT`` 控制所有 LLM 共享的并发上限（立即生效）。
三者独立，按需调整。

**幂等**：``ON CONFLICT (key) DO NOTHING``，允许多次 upgrade；prod 已存在行不动。

**两库同步**：升级到 head；prod 与 qa_metadata_test 都需要。

Revision ID: 0080
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision: str = "0080_llm_concurrency_config"
down_revision: str | None = "0079_chat_concurrency_params"
branch_labels = None
depends_on = None


_SEED_SQL = text(
    """
    INSERT INTO system_config (key, value, description, updated_time) VALUES
      (
        'LLM_CONCURRENCY_LIMIT', '20',
        '全局 LLM 并发上限（所有模型共享的 asyncio.Semaphore limit）。'
        '50 用户时建议 20-30；过低会拖慢并发响应，过高会被 LLM provider 429。'
        '立即生效（admin 改值后下一个请求即用新 limit）。'
        '合法值: 正整数 | 默认: 20',
        now()
      )
    ON CONFLICT (key) DO NOTHING
    """
)


def upgrade() -> None:
    op.execute(_SEED_SQL)


def downgrade() -> None:
    # 仅在「未被人修改过」时撤；线上若有手动调整过，留着更安全——与 0079 同模式。
    op.execute(
        text(
            "DELETE FROM system_config WHERE key = 'LLM_CONCURRENCY_LIMIT' "
            "AND value = '20'"
        )
    )