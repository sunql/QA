"""放宽 ``audit_log.action`` CHECK 约束：新增 P5 工作流 / 审批动作（0152）。

**触发**：P5 审批面（D-6 / W4）要求「每步可追溯」——
提交 / 发起审批 / 通过 / 拒绝 / 超时 / 补偿都要落 ``audit_log``。
``AuditService._VALID_ACTIONS`` 与 DB CHECK 都是白名单 ⇒ 只改服务层会被
DB 拒收（``23514`` CHECK 违例 ⇒ 审批写入口整条 500）。

**新增动作**（6 个）：

| 动作 | 语义 |
|---|---|
| ``APPROVE`` | 审批通过 |
| ``REJECT`` | 审批拒绝 |
| ``COMPENSATE`` | 失败补偿（逆序回滚已成功的命令） |
| ``WORKFLOW_SUBMIT`` | 提交工作流实例 |
| ``WORKFLOW_APPROVAL_REQUESTED`` | 发起一轮审批 |
| ``WORKFLOW_TIMEOUT`` | 审批超时（任务转 TIMEOUT、实例转 FAILED） |

**变更**：DROP 旧 CK + ADD 新 CK（放宽 CHECK 而非加列 —— 与 ``0083`` /
``0110`` / ``0148`` 同一套做法）。``action`` 列是 VARCHAR(32)，
``WORKFLOW_APPROVAL_REQUESTED``（26 字符）无需改列宽。

**与 ORM 同形**：``app/domain/models.py`` 的 ``AuditLog.__table_args__``
已同步（alembic 不比较 CHECK 表达式 ⇒ 两侧都得手写，改一处不改另一处
只会静默漂移）。

**幂等性**：降级反向（DROP 新 CK + ADD 旧 CK）。

**两库同步**：prod + test（迁移链 0151 → 0152）。

Revision ID: 0152
"""

from __future__ import annotations

from alembic import op

revision = "0152"
down_revision = "0151"
branch_labels = None
depends_on = None


_NEW_CK = (
    "action IN ('CREATE','UPDATE','DELETE','RESTORE','REUSE',"
    "'auth.login','auth.login_failed','auth.logout',"
    "'auth.password_changed','user.password_reset',"
    "'APPROVE','REJECT','COMPENSATE','WORKFLOW_SUBMIT',"
    "'WORKFLOW_APPROVAL_REQUESTED','WORKFLOW_TIMEOUT')"
)
_OLD_CK = (
    "action IN ('CREATE','UPDATE','DELETE','RESTORE','REUSE',"
    "'auth.login','auth.login_failed','auth.logout',"
    "'auth.password_changed','user.password_reset')"
)


def upgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_action", "audit_log", _NEW_CK)


def downgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_action", "audit_log", _OLD_CK)
