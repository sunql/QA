"""seed CLASS_FILTER_MAX_CLASSES 到 system_config（feat-system-params）。

**背景**：``_CLASS_FILTER_MAX_CLASSES = 30`` 原本写死在 chat_service.py，召回扩边
超过 30 类即触发前端「本次命中的数据表已达上限（30 张）」警告，用户无法调整。
系统参数菜单（admin/system-config）已存在，本迁移把该变量从硬编码转为
system_config 行 + admin UI 可调。

**Description 写「可调范围说明」**（用户需求中的「可以设置什么值的说明」）：
30 是经验值；增大可容纳更多关联表但会降低 LLM 选表精度；建议不超过 50。

**幂等**：``WHERE NOT EXISTS`` 守卫，已有值不改——保护线上已调整过的配置。

**两库同步**：升级到 head；prod 已存在的行不动，仅补缺失行。

Revision ID: 0078
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision: str = "0078_system_config_class_filter"
down_revision: str | None = "0077_wiki_compile_tables"
branch_labels = None
depends_on = None

_SEED_SQL = text(
    """
    INSERT INTO system_config (key, value, description, updated_time)
    SELECT
        'CLASS_FILTER_MAX_CLASSES',
        '30',
        'NL2SQL 类召回扩边后的 schema 类总量上限（防 prompt 无界膨胀）。'
        '30 是经验值；增大可容纳更多关联表但会降低 LLM 选表精度；'
        '建议不超过 50。修改后立即对新问句生效，无需重启。',
        now()
    WHERE NOT EXISTS (
        SELECT 1 FROM system_config WHERE key = 'CLASS_FILTER_MAX_CLASSES'
    )
"""
)


def upgrade() -> None:
    op.execute(_SEED_SQL)


def downgrade() -> None:
    # 仅在「未被人修改过」时撤；线上若有手动调整过，留着更安全——此处严格对称
    # 用条件 DELETE，撤迁移会把行带走（保守）。
    op.execute(
        text(
            "DELETE FROM system_config WHERE key = 'CLASS_FILTER_MAX_CLASSES' "
            "AND value = '30'"
        )
    )