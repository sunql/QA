"""A4-0：审批面放开 ``instance_id`` + 幂等键换成 subject 三元组（0159）。

**触发**：WP-ONT-AI A4「提案与发布 / 四眼审核」计划复用 P5 审批面 ——
审批任务落在 ``approval_task``，用 ``subject_type = 'ontology_proposal'``
标识被审对象（见 ``Harness/changes/2026-11-16-ontology-discovery-ai/plan.md`` §A4-3）。
但按现状代码**走不通**：

1. ``approval_task.instance_id`` 是 **NOT NULL + FK** → ``workflow_instance``
   ⇒ 一条审批任务**必须**挂在某个工作流实例上；
2. ``WorkflowApprovalService._createRoundTasks`` 把 ``subjectId`` **写死**成
   ``str(instance.id)`` ⇒ 被审对象恒等于工作流实例。

而「本体变更提案」**没有工作流实例可挂**（方案 B「给每个提案造包装实例」语义污染；
方案 C「另建 proposal_approval 表」等于把四眼 / 超时 / 幂等 / 审计重写一遍，违反 DRY）。

**选定方案 A**：``instance_id`` 放开为可空；工作流路径**两列都填**，
提案路径**只填 subject 三元组**。

**幂等键为什么必须换**（本迁移最关键的一处）：

- 幂等键**不能含可空列** —— PG 里 ``NULL`` 互不相等 ⇒
  ``UNIQUE(instance_id, matrix_code)`` 在 ``instance_id IS NULL`` 时**静默失效**
  （同一个提案的同一矩阵条目能落两行 ⇒ 重复发起 = 两份待审 ⇒ 四眼失效）。
- 换成 ``UNIQUE(subject_type, subject_id, matrix_code)``：三列**全部 NOT NULL**
  ⇒ 幂等在两条路径上都成立。
- **工作流路径的幂等语义逐字不变**：该路径恒有
  ``subject_id = str(instance.id)`` ⇒ ``(subject_type, subject_id, matrix_code)``
  与旧的 ``(instance_id, matrix_code)`` **一一对应**（同一实例同一矩阵条目仍只落一条）。

**变更**（``approval_task`` 一张表，只放宽不加不减）：

- ``instance_id``：``NOT NULL`` → nullable（FK 与 ``RESTRICT`` **不变**；
  ``NULL`` 即「这条审批不挂工作流实例」）。
- 唯一约束：``uq_approval_task_matrix(instance_id, matrix_code)``
  → ``uq_approval_task_subject(subject_type, subject_id, matrix_code)``。

**降级**：反向 —— 先删新唯一键、恢复旧唯一键，再把 ``instance_id`` 收回 ``NOT NULL``。
若库里已有**提案路径**的任务行（``instance_id IS NULL``），降级会因
``NOT NULL`` 冲突失败 ⇒ 这是**预期行为**（数据已超出旧口径，先导出再降级）。

**两库同步**：prod + test（迁移链 0158 → 0159）。

Revision ID: 0159
"""

from __future__ import annotations

from alembic import op

revision = "0159"
down_revision = "0158"
branch_labels = None
depends_on = None

_TABLE_APPROVAL = "approval_task"

# 旧幂等键（实例 + 矩阵条目）→ 新幂等键（被审对象三元组）。见模块 docstring。
_OLD_UNIQUE = "uq_approval_task_matrix"
_NEW_UNIQUE = "uq_approval_task_subject"
_NEW_UNIQUE_COLUMNS = ("subject_type", "subject_id", "matrix_code")


def upgrade() -> None:
    op.alter_column(_TABLE_APPROVAL, "instance_id", nullable=True)

    op.drop_constraint(_OLD_UNIQUE, _TABLE_APPROVAL, type_="unique")
    op.create_unique_constraint(_NEW_UNIQUE, _TABLE_APPROVAL, list(_NEW_UNIQUE_COLUMNS))


def downgrade() -> None:
    op.drop_constraint(_NEW_UNIQUE, _TABLE_APPROVAL, type_="unique")
    op.create_unique_constraint(_OLD_UNIQUE, _TABLE_APPROVAL, ["instance_id", "matrix_code"])

    # ⚠️ 库里有提案路径的行（instance_id IS NULL）时这一步会失败 —— 预期行为。
    op.alter_column(_TABLE_APPROVAL, "instance_id", nullable=False)
