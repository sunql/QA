"""seed 魔数治理（Phase 2 首批）5 个编译期常量到 system_config。

**背景**：chat_service 拆分后，类召回/历史上下文里 5 个编译期阈值仍写死在
chat_recall.py / chat_context.py。本迁移把它们迁入 system_config 行 + admin UI 可调，
与已治理的 CLASS_FILTER_MAX_CLASSES（0078）/ LLM_CONCURRENCY_LIMIT（0080）同口径：
运行期每次现读、缺席/格式错/非正返硬编码默认、admin 改值后立即生效。

首批 5 项（Phase 2「easy tier」，usage site 已有 session，无需穿透会话）：
- CLASS_FILTER_TOP_K（15）：类裁剪向量检索 topK
- CLASS_FILTER_HIT_MATCH_MIN（0.5）：命中可解析比例下限（低于告警，0.0 恒不告警）
- CONTEXT_CONTENT_SEGMENT_LIMIT（500）：单条历史消息正文字符上限
- CONTEXT_SQL_SEGMENT_LIMIT（500）：单条历史消息携带 SQL 字符上限
- CONTEXT_PROMPT_CHAR_BUDGET（4000）：历史上下文拼接总预算

**幂等**：``ON CONFLICT (key) DO NOTHING``，允许多次 upgrade；prod 已存在行不动。

**两库同步**：升级到 head；prod 与 qa_metadata_test 都需要。

Revision ID: 0087
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision: str = "0087_magic_number_config"
down_revision: str | None = "0086_supplier_name_trigram"
branch_labels = None
depends_on = None

_SEED_SQL = text(
    """
    INSERT INTO system_config (key, value, description, updated_time) VALUES
      (
        'CLASS_FILTER_TOP_K', '15',
        '类裁剪向量检索 topK（召回相关本体类的条数）。'
        '增大召回更多候选类但会稀释命中精度；建议 10-30。'
        '合法值: 正整数 | 默认: 15',
        now()
      ),
      (
        'CLASS_FILTER_HIT_MATCH_MIN', '0.5',
        '类裁剪命中中可解析为真实类的比例下限（低于该值告警，防检索漂移）。'
        '0.0 表示永不告警；1.0 表示全命中才算通过。'
        '合法值: [0.0, 1.0] | 默认: 0.5',
        now()
      ),
      (
        'CONTEXT_CONTENT_SEGMENT_LIMIT', '500',
        '注入历史上下文时单条消息正文的字符上限。'
        '超过会被截断并加省略号；调小可压低每轮 prompt 成本。'
        '合法值: 正整数 | 默认: 500',
        now()
      ),
      (
        'CONTEXT_SQL_SEGMENT_LIMIT', '500',
        '注入历史上下文时单条消息携带的历史 SQL 字符上限（与正文分开限量）。'
        '追问 REFINE 依赖历史 SQL 可见性，调太小时多轮追问精度下降。'
        '合法值: 正整数 | 默认: 500',
        now()
      ),
      (
        'CONTEXT_PROMPT_CHAR_BUDGET', '4000',
        '历史上下文拼接后的总字符预算。应 ≥ 单块上限之和，否则只剩最新一块。'
        '调小降低每轮 prompt 成本，调大保留更多历史。'
        '合法值: 正整数 | 默认: 4000',
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
            "'CLASS_FILTER_TOP_K', 'CLASS_FILTER_HIT_MATCH_MIN', "
            "'CONTEXT_CONTENT_SEGMENT_LIMIT', 'CONTEXT_SQL_SEGMENT_LIMIT', "
            "'CONTEXT_PROMPT_CHAR_BUDGET')"
        )
    )
