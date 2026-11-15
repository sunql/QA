"""Agent 编排 run 钉住语义发布：`agent_orchestration_run` 三列（P3c D26）。

P3c D26（spec: docs/update/1109/智能采购平台升级计划_日级排程_1109.md §8）：
run 创建时把「当前激活的语义发布」冻结进三个可空列 ⇒ 运行期发布变更不影响该 run，
同一 run 重放 ⇒ 同一发布 ⇒ 同一口径（与 P2 D16 的 `session_query_state.semantic_release_id`
同一思想，但钉住对象是**编排 run** 而不是会话）。

三列全部**可空** ⇒ 存量旧行（NULL）**语义不变**（NULL = 未钉住 = 照旧取当前 ACTIVE，
与今天一致）；downgrade 直接删列。

- `semantic_release_id`：钉住的发布主键（解析时的身份）；
- `semantic_release_version`：版本号**冗余快照**（发布行将来被删，run 仍可读）；
- `semantic_manifest_hash`：manifest 哈希**冗余快照**（完整性核对：run 与它
  那一版发布对不对得上）。

**手写**（禁 autogenerate）：只加法 DDL、零副作用；列 `comment` 与
`app/domain/agent_orchestration_models.py` 里 ORM 的 `comment` **必须逐字一致**
（autogenerate 会比对列注释，两边不一致 ⇒ `alembic check` 报漂移；
该一致性由 `app/tests/unit/test_agent_run_semantic_pin.py` 的单测双向钉住）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None

# 与 `app/domain/agent_orchestration_models.py` 的 ORM 列注释逐字一致
# （单测 test_agent_run_semantic_pin.py 双向钉住）。
COLUMN_COMMENTS = {
    "semantic_release_id": "0145，P3c D26：本 run 钉住的语义发布 id（NULL = 未钉住 = 照旧取当前 ACTIVE）",
    "semantic_release_version": "0145，P3c D26：钉住发布的版本号快照（冗余，防发布行被删后 run 不可读）",
    "semantic_manifest_hash": "0145，P3c D26：钉住发布的 manifest 哈希快照（完整性核对用）",
}


def upgrade() -> None:
    op.add_column(
        "agent_orchestration_run",
        sa.Column(
            "semantic_release_id",
            sa.Integer(),
            nullable=True,
            comment=COLUMN_COMMENTS["semantic_release_id"],
        ),
    )
    op.add_column(
        "agent_orchestration_run",
        sa.Column(
            "semantic_release_version",
            sa.String(length=64),
            nullable=True,
            comment=COLUMN_COMMENTS["semantic_release_version"],
        ),
    )
    op.add_column(
        "agent_orchestration_run",
        sa.Column(
            "semantic_manifest_hash",
            sa.String(length=64),
            nullable=True,
            comment=COLUMN_COMMENTS["semantic_manifest_hash"],
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_orchestration_run", "semantic_manifest_hash")
    op.drop_column("agent_orchestration_run", "semantic_release_version")
    op.drop_column("agent_orchestration_run", "semantic_release_id")
