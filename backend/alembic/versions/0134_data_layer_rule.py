"""数据层规则表：data_layer_rule（feat-data-domain-layer 方案 2）。

Agent 数据层词表 + 表名前缀识别规则收敛到单一 DB SSOT：
- pattern 非空 = 前缀规则（长前缀先匹配，priority 小者优先）；
- pattern 为空 = 手工声明层（不可由前缀探测，如 FEATURE 特征库层——
  存量 agent_definition / agent_access_policy 在用，必须保留合法）。

seed 7 行与 chat_recall._getClassLayer 现行为逐字对齐
（0133/0134 后 recall 经 layerRuleRegistry 读同源规则，见 chat_recall 改造）。

NULL pattern 的唯一性用双 partial unique index，不用 PG15 NULLS NOT DISTINCT
（prod PG 版本无保证）：NULL 在默认唯一索引里互不相等，会把手工层约束打穿。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None

# (pattern, layer, priority)；pattern 为空的行 = 手工声明层。
# priority：ODS_DIM_ 必须先于 ODS_ 匹配（长前缀优先），其余按 _getClassLayer 原顺序。
_SEED_RULES = (
    ("ODS_DIM_", "ODS_DICT", 10),
    ("ADS_", "ADS", 20),
    ("DWS_", "DWS", 30),
    ("DWD_", "DWD", 40),
    ("DIM_", "DIM", 50),
    ("ODS_", "ODS_BUSINESS", 60),
    (None, "FEATURE", 90),
)


def upgrade() -> None:
    op.create_table(
        "data_layer_rule",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("layer", sa.String(length=20), nullable=False),
        sa.Column("pattern", sa.String(length=100), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("datasource_type", sa.String(length=20), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_layer_rule_pattern", "data_layer_rule",
        ["layer", "pattern"], unique=True,
        postgresql_where=sa.text("pattern IS NOT NULL"),
    )
    op.create_index(
        "uq_layer_rule_manual", "data_layer_rule",
        ["layer"], unique=True,
        postgresql_where=sa.text("pattern IS NULL"),
    )

    for pattern, layer, priority in _SEED_RULES:
        op.execute(
            sa.text(
                "INSERT INTO data_layer_rule (layer, pattern, priority, enabled) "
                "SELECT :layer, :pattern, :priority, TRUE "
                # 幂等：仅当同 layer+pattern 不存在时插入（重放安全）
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM data_layer_rule WHERE layer = :layer"
                "    AND ((:pattern IS NULL AND pattern IS NULL) OR pattern = :pattern)"
                ")"
            # pattern 可能为 None：显式声明 VARCHAR 类型，否则 asyncpg 对 NULL
            # 参数无法推断（"text versus character varying"）。
            ).bindparams(
                sa.bindparam("layer", type_=sa.String()),
                sa.bindparam("pattern", type_=sa.String()),
                sa.bindparam("priority", type_=sa.Integer()),
            ).bindparams(layer=layer, pattern=pattern, priority=priority)
        )


def downgrade() -> None:
    op.drop_index("uq_layer_rule_manual", table_name="data_layer_rule")
    op.drop_index("uq_layer_rule_pattern", table_name="data_layer_rule")
    op.drop_table("data_layer_rule")
