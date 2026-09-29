"""wiki_rule_executable + process_workflow - 机制 5 的结构化产物表（Phase 8 M6）。

新增两张表，承载「这条知识已经从自然语言升级成机器可读结构」的**产物**：

- ``wiki_rule_executable``：可执行业务规则（条件 + 动作）。Agent 运行时直读。
- ``process_workflow``：结构化流程（步骤序列 + 触发条件）。

**刻意不 sync 到 ``feature_rule``**（方案已拍板）：后者是特征工程的阈值表
（``feature_name`` + ``operator/threshold_value/unit``，无表达式列），语义是
「某个特征在某个阈值上」，与本表的「一条带多条件与动作的业务规则」不是一回事。
硬塞过去要么丢条件、要么把 feature_rule 撑成通用表达式表，两头都不对。

``page_id`` 是 **UNIQUE**（一条知识最多一条可执行规则 / 一个流程）：产物是那条
知识的**当前**结构化形态，不是历史版本流。历史留在 ``learning_feedback``（谁在
什么时候接受了什么建议）。故重复物化走 ``ON CONFLICT DO UPDATE`` 覆盖，而不是
新增一行 —— 唯一约束在这里是「一条知识一份产物」的强制表达。

``dry_run_examples`` 随规则一起存：dry-run 的样例是**审核这条规则的人**给的期望，
属于规则本身的一部分（下次改规则时要能看出原来期望什么），不该只活在请求体里。

建表用 ``IF NOT EXISTS`` 幂等守卫，与 0053-0057 同模式。

Revision ID: 0058
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0058_structure_artifact_tables"
down_revision: str | None = "0057_conflict_suggestion_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_rule_executable (
            id                BIGSERIAL PRIMARY KEY,
            page_id           VARCHAR(64) NOT NULL UNIQUE
                                REFERENCES wiki_page(page_id) ON DELETE CASCADE,
            rule_kind         VARCHAR(30)  NOT NULL,
            rule_expression   JSONB        NOT NULL,
            target_entity     VARCHAR(100),
            dry_run_examples  JSONB,
            authority_chain   VARCHAR(10)[],
            version           VARCHAR(30)  NOT NULL DEFAULT 'v1.0',
            created_time      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_time      TIMESTAMP WITH TIME ZONE
        )
        """
    )
    # Agent 侧最常用的形态：按目标实体捞出全部适用规则。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wiki_rule_executable_target "
        "ON wiki_rule_executable (target_entity)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS process_workflow (
            id                BIGSERIAL PRIMARY KEY,
            page_id           VARCHAR(64) NOT NULL UNIQUE
                                REFERENCES wiki_page(page_id) ON DELETE CASCADE,
            workflow_version  VARCHAR(30) NOT NULL DEFAULT 'v1.0',
            steps             JSONB NOT NULL DEFAULT '[]'::jsonb,
            trigger_condition TEXT,
            applicable_scope  JSONB,
            created_time      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_time      TIMESTAMP WITH TIME ZONE
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS process_workflow")
    op.execute("DROP TABLE IF EXISTS wiki_rule_executable")
