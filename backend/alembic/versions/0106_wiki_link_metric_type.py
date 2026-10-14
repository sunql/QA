"""wiki_ontology_link 的 ontology_type 放开到 metric（C 档：指标入链，2026-09-30）。

**触发**：Wiki 链接管理只能绑 class / property，本体指标不可入链。调查发现类型被
写死在 6 处，其中 DB 层就是本约束 ``chk_link_type``。

**变更**：drop 后按三个值重建同名约束（PostgreSQL 的 CHECK 不能就地改，必须 drop +
add）。约束名保持不变，避免下游脚本/文档引用失效。

**安全性**：当前生产 ``wiki_ontology_link`` **0 行**，重建约束不会因存量数据违反而失败；
且新集合是旧集合的**超集**，任何存量行都必然满足新约束。

**降级是破坏性的**：downgrade 必须先删除 metric 行才能重建两值约束（否则 ADD
CONSTRAINT 会失败）。沿用 0098 的先例（其 downgrade 亦删除不合规行），此处显式删除
并把后果写在这里 —— 丢失的是「指标链接」这一新类型的配置，class/property 不受影响。

**两库同步**：prod + test 都要 upgrade。

Revision ID: 0106
"""

from __future__ import annotations

from alembic import op

revision: str = "0106"
down_revision: str | None = "0105"
branch_labels = None
depends_on = None

_CONSTRAINT = "chk_link_type"
_OLD_VALUES = ("class", "property")
_NEW_VALUES = ("class", "property", "metric")


def _sql(values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"ontology_type IN ({joined})"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "wiki_ontology_link", type_="check")
    op.create_check_constraint(_CONSTRAINT, "wiki_ontology_link", _sql(_NEW_VALUES))


def downgrade() -> None:
    # 先清掉新类型行，否则旧的两值约束加不回去（详 docstring）
    op.execute("DELETE FROM wiki_ontology_link WHERE ontology_type = 'metric'")
    op.drop_constraint(_CONSTRAINT, "wiki_ontology_link", type_="check")
    op.create_check_constraint(_CONSTRAINT, "wiki_ontology_link", _sql(_OLD_VALUES))
