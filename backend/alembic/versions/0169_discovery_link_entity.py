"""发现评审：候选/问题补「关联本体对象」（WP-ONT-AI G5，0169）。

评审需要把「采纳的候选 / 发现的问题」挂到**本体里的具体对象**上（类/属性/指标），
否则判定与本体之间没有可追溯的链路。

只存弱引用（类型 + 自然键），不建外键：
- 类/属性/指标都是可被软删除/重命名的治理对象，硬外键会让历史判定悬空；
- 自然键（`class_name` / `class_name.property_name` / `metric_name`）人与脚本都能读。

纯加法；列注释与 ORM 逐字一致（单测钉住）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0169"
down_revision = "0168"
branch_labels = None
depends_on = None

_CANDIDATE_TYPE_COMMENT = "0169，WP-ONT-AI G5：关联的本体对象类型（ONTOLOGY_CLASS / ONTOLOGY_PROPERTY / ONTOLOGY_METRIC）"
_CANDIDATE_CODE_COMMENT = "0169，WP-ONT-AI G5：关联对象自然键（类名 / 类名.属性名 / 指标名）"
_FINDING_TYPE_COMMENT = "0169，WP-ONT-AI G5：问题关联的本体对象类型"
_FINDING_CODE_COMMENT = "0169，WP-ONT-AI G5：问题关联对象自然键"


def upgrade() -> None:
    for table, typeComment, codeComment in (
        ("discovery_candidate", _CANDIDATE_TYPE_COMMENT, _CANDIDATE_CODE_COMMENT),
        ("discovery_finding", _FINDING_TYPE_COMMENT, _FINDING_CODE_COMMENT),
    ):
        op.add_column(
            table,
            sa.Column("linked_entity_type", sa.String(length=32), nullable=True, comment=typeComment),
        )
        op.add_column(
            table,
            sa.Column("linked_entity_code", sa.String(length=200), nullable=True, comment=codeComment),
        )


def downgrade() -> None:
    for table in ("discovery_finding", "discovery_candidate"):
        op.drop_column(table, "linked_entity_code")
        op.drop_column(table, "linked_entity_type")
