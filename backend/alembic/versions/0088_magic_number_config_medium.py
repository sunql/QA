"""seed 魔数治理（Phase 2 第二批）4 个编译期常量到 system_config。

**背景**：续 0087 首批。本批治理 few-shot 检索与状态历史快照的 4 个阈值
（均从 chat_recall.py / chat_context.py 硬编码迁入 system_config 行 + admin UI 可调）。

第二批 4 项（Phase 2「medium tier」，usage site 需穿透 session —— `_buildFewShot`
加 session 参数、`_buildStatePrompt` 加 field_limit 参数）：
- FEW_SHOT_TOP_K（3）：历史相似 SQL few-shot 检索条数
- FEW_SHOT_SIMILARITY_MIN（0.6）：相似度下限（低于视为噪音，0.0 关闭过滤）
- FEW_SHOT_EXAMPLE_LIMIT（400）：单条示例 question/sql 字符上限
- STATE_HISTORY_FIELD_LIMIT（500）：状态历史快照单条 q/s 字符上限

**幂等**：``ON CONFLICT (key) DO NOTHING``，允许多次 upgrade；prod 已存在行不动。

**两库同步**：升级到 head；prod 与 qa_metadata_test 都需要。

Revision ID: 0088
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision: str = "0088_magic_number_config_medium"
down_revision: str | None = "0087_magic_number_config"
branch_labels = None
depends_on = None

_SEED_SQL = text(
    """
    INSERT INTO system_config (key, value, description, updated_time) VALUES
      (
        'FEW_SHOT_TOP_K', '3',
        '历史相似 SQL few-shot 的检索条数（注入即 token 成本，取小值）。'
        '增大注入更多示例但推高每轮 prompt 成本。'
        '合法值: 正整数 | 默认: 3',
        now()
      ),
      (
        'FEW_SHOT_SIMILARITY_MIN', '0.6',
        'few-shot 相似度下限（低于该值的命中视为噪音，不注入）。'
        '0.0 表示关闭过滤（全部注入）；1.0 表示只注入完全匹配。'
        '合法值: [0.0, 1.0] | 默认: 0.6',
        now()
      ),
      (
        'FEW_SHOT_EXAMPLE_LIMIT', '400',
        '单条 few-shot 示例的 question/sql 字符上限（few-shot 每阶段重复注入）。'
        '调小可压低 NL2SQL 输入 token；调大保留更长 SQL 示例。'
        '合法值: 正整数 | 默认: 400',
        now()
      ),
      (
        'STATE_HISTORY_FIELD_LIMIT', '500',
        '状态历史快照（recent_rounds）单条 q/s 字符上限。'
        '防止超长问题或 SQL 撑爆 NL2SQL prompt。'
        '合法值: 正整数 | 默认: 500',
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
            "DELETE FROM system_config WHERE key IN ("
            "'FEW_SHOT_TOP_K', 'FEW_SHOT_SIMILARITY_MIN', "
            "'FEW_SHOT_EXAMPLE_LIMIT', 'STATE_HISTORY_FIELD_LIMIT')"
        )
    )
