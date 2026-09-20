"""放宽 ``audit_log.action`` CHECK 约束（feat-user-auth，2026-09-20）。

**触发**：feat-user-auth 引入 5 个新 audit action（auth.login /
auth.login_failed / auth.logout / auth.password_changed /
user.password_reset）。原约束只允许 CREATE / UPDATE / DELETE → DB 写入抛
``CheckViolation``。

**变更**：DROP 旧 CK + ADD 新 CK（白名单 + 新增 5 项）。

**幂等性**：降级反向（DROP 新 CK + ADD 旧 CK）。

**两库同步**：prod + test。

Revision ID: 0083
"""

from __future__ import annotations

from alembic import op

import sqlalchemy as sa

revision: str = "0083_audit_log_auth_actions"
down_revision: str | None = "0082_user_auth_tables"
branch_labels = None
depends_on = None


_NEW_CK = (
    "action IN ('CREATE','UPDATE','DELETE',"
    "'auth.login','auth.login_failed','auth.logout',"
    "'auth.password_changed','user.password_reset')"
)
_OLD_CK = "action IN ('CREATE','UPDATE','DELETE')"


def upgrade() -> None:
    # 放宽 action 列：VARCHAR(20) → VARCHAR(32)，容纳 auth.password_changed (21)
    op.alter_column(
        "audit_log", "action",
        existing_type=sa.String(20),
        type_=sa.String(32),
        existing_nullable=False,
    )
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_action", "audit_log", _NEW_CK
    )


def downgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_action", "audit_log", _OLD_CK
    )
    op.alter_column(
        "audit_log", "action",
        existing_type=sa.String(32),
        type_=sa.String(20),
        existing_nullable=False,
    )