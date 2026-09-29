"""seed 魔数治理（Phase 2 hard tier）6 个编译期常量到 system_config。

**背景**：续 0087/0088。本批治理 nl2sql 模块（无状态纯函数管线）的 6 个阈值，
usage site 在 nl2sql_service.py / nl2sql_plan.py / nl2sql_schema.py /
nl2sql_refs.py / nl2sql_refine.py。与 easy/medium tier 同口径：
`_getXxx(session)` 现读 + `text()` 直写 key，int getter 非正返 _DEFAULT。

hard tier 6 项（编排层 generateSql / generateValidatedPlan 现读后
作为可选参数透传给纯函数 buildSchemaText / validatePlan / _extractLimit）：
- REFINE_MAX_LIMIT（1000）：REFINE 行数上限（钳制超大 LIMIT/FETCH）
- OWNER_HINT_MAX_CLASSES（3）：属性归属提示最多列出的拥有类数
- CRITICAL_DIGEST_MAX_ITEMS（50）：关键过滤口径摘要条数上限
- CRITICAL_DIGEST_MAX_DESC_CHARS（200）：摘要单条描述字符上限
- VALUE_SAMPLE_VALUE_MAX（30）：值域采样单值字符上限
- NL2SQL_MAX_TOKENS（2048）：NL2SQL 单次 LLM 调用 token 上限

**幂等**：``ON CONFLICT (key) DO NOTHING``，允许多次 upgrade；prod 已存在行不动。

**两库同步**：升级到 head；prod 与 qa_metadata_test 都需要。

Revision ID: 0089
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision: str = "0089_magic_number_config_hard"
down_revision: str | None = "0088_magic_number_config_medium"
branch_labels = None
depends_on = None

_SEED_SQL = text(
    """
    INSERT INTO system_config (key, value, description, updated_time) VALUES
      (
        'REFINE_MAX_LIMIT', '1000',
        'REFINE 行数上限：钳制超大 LIMIT/FETCH，避免拖慢查询规划。'
        '追问「前 999999999 条」会被钳到该值。'
        '合法值: 正整数 | 默认: 1000',
        now()
      ),
      (
        'OWNER_HINT_MAX_CLASSES', '3',
        '属性归属提示最多列出的拥有类数量（避免 schema 类多时提示过长挤占重试 token）。'
        '增大列出更多归属类，但会拉长重试反馈。'
        '合法值: 正整数 | 默认: 3',
        now()
      ),
      (
        'CRITICAL_DIGEST_MAX_ITEMS', '50',
        '关键过滤口径摘要单次渲染的最大条数（防恶意管理员堆 description 撑爆 prompt）。'
        '增大保留更多口径摘要，但会推高 plan 阶段输入 token。'
        '合法值: 正整数 | 默认: 50',
        now()
      ),
      (
        'CRITICAL_DIGEST_MAX_DESC_CHARS', '200',
        '关键过滤口径摘要单条描述的最大字符数（防单条 500 字被全文灌入摘要）。'
        '增大保留更长口径说明，但会推高 plan 阶段输入 token。'
        '合法值: 正整数 | 默认: 200',
        now()
      ),
      (
        'VALUE_SAMPLE_VALUE_MAX', '30',
        '值域采样单值在 schema 文本中的字符上限（超长截断防止 prompt 膨胀）。'
        '调小压低 schema 文本长度；调大保留更完整的值域示例。'
        '合法值: 正整数 | 默认: 30',
        now()
      ),
      (
        'NL2SQL_MAX_TOKENS', '2048',
        'NL2SQL 单次 LLM 调用的 token 上限。回复达到上限时 SQL 可能被截断，'
        '检测到后翻倍预算重试（截断重试预算封顶 = 该值 × 2）。'
        '调小降低单轮成本，但长 SQL 更易触发截断重试。'
        '合法值: 正整数 | 默认: 2048',
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
            "'REFINE_MAX_LIMIT', 'OWNER_HINT_MAX_CLASSES', "
            "'CRITICAL_DIGEST_MAX_ITEMS', 'CRITICAL_DIGEST_MAX_DESC_CHARS', "
            "'VALUE_SAMPLE_VALUE_MAX', 'NL2SQL_MAX_TOKENS')"
        )
    )
