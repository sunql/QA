"""seed LLM_CACHE_HIT_MULTIPLIER（DeepSeek prompt cache 命中折扣倍数）。

**背景**：续 feat-token-cache。前序 commit `f0a2758` / `649ae2b` 让
`_costFor` 读取 `LlmResponse.cachedTokens` 并按差额计费（命中部分免费）；
本批 multiplier 精修（4-1）将"差额计费"扩展为"差额 + 命中部分按 miss
单价的 multiplier 计费"，对齐 DeepSeek 实际账单模型（cache hit 仍按
miss 的 ~1/4 计费，不是 0）。

`LLM_CACHE_HIT_MULTIPLIER` 默认 0.25 — 对齐 DeepSeek 当前价（2024 定价 miss/4）。
合法值 [0, 1]：
- 0   = 命中免费（旧口径，回滚开关）
- 0.25 = DeepSeek 当前价
- 1   = 不打折（无 cache 优惠）

读路径在 `app.services.nl2sql_service._readFloatConfig`（4-1 新增 helper，
非正/非 [0,1] 返 default）。

**幂等**：``ON CONFLICT (key) DO NOTHING``，允许多次 upgrade；prod 已存在
行不动（admin 可在线调）。

**两库同步**：升级到 head；prod 与 qa_metadata_test 都需要。

Revision ID: 0090
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision: str = "0090_llm_cache_hit_multiplier"
down_revision: str | None = "0089_magic_number_config_hard"
branch_labels = None
depends_on = None

_SEED_SQL = text(
    """
    INSERT INTO system_config (key, value, description, updated_time) VALUES
      (
        'LLM_CACHE_HIT_MULTIPLIER', '0.25',
        'DeepSeek prompt cache 命中部分按 miss 单价的该倍数计费（billable = (prompt - cached) + cached × multiplier）。'
        '默认 0.25 对齐 DeepSeek 当前价（cache hit 仍按 miss 1/4 计，不是免费）。'
        '设为 0 = 命中完全免费（旧差额口径，回滚开关）；'
        '设为 1 = 不打折（无 cache 优惠）。'
        '合法值: [0, 1] | 默认: 0.25',
        now()
      )
    ON CONFLICT (key) DO NOTHING
    """
)


def upgrade() -> None:
    op.execute(_SEED_SQL)


def downgrade() -> None:
    op.execute(
        text(
            "DELETE FROM system_config WHERE key = 'LLM_CACHE_HIT_MULTIPLIER'"
        )
    )