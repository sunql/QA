"""放宽 ``audit_log.action`` CHECK 约束（fix-class-tombstone-restore，2026-10-03）。

**触发**：新增本体类恢复（``POST /ontology/classes/{id}/restore``），audit
动作 ``RESTORE`` 需要落库。原 CHECK 白名单不含 RESTORE → 写入抛
``CheckViolationError``（实测）。

**变更**：DROP 旧 CK + ADD 新 CK（白名单 + RESTORE）。

**幂等性**：降级反向（DROP 新 CK + ADD 旧 CK）。

**两库同步**：prod + test（容器启动 / 部署脚本自动迁移）。
"""

from __future__ import annotations

from alembic import op

revision: str = "0110"
down_revision: str | None = "0109"
branch_labels = None
depends_on = None


_NEW_CK = (
    "action IN ('CREATE','UPDATE','DELETE','RESTORE',"
    "'auth.login','auth.login_failed','auth.logout',"
    "'auth.password_changed','user.password_reset')"
)
_OLD_CK = (
    "action IN ('CREATE','UPDATE','DELETE',"
    "'auth.login','auth.login_failed','auth.logout',"
    "'auth.password_changed','user.password_reset')"
)


def upgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_action", "audit_log", _NEW_CK
    )


def downgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_action", "audit_log", _OLD_CK
    )