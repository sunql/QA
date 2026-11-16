"""放宽 ``audit_log.action`` CHECK 约束：新增 ``REUSE``（WP-ONT-AI A1，0148）。

**触发**：本体发现域的快照采集是**内容寻址**的——同一源、同一内容 ⇒ 复用既有行
（**没有新建**）。审计若把复用记成 `CREATE`，「新增了多少行」的台账就会虚高
（代码审查 MEDIUM-8）。`AuditService._VALID_ACTIONS` 与 DB CHECK 都只认
CREATE / UPDATE / DELETE / RESTORE / auth.* ⇒ 记 `REUSE` 会被服务层与 DB 层
双双拒绝。

**变更**：DROP 旧 CK + ADD 新 CK（白名单 + `REUSE`）。

参照 `0083_audit_log_auth_actions.py`（同一套做法：放宽 CHECK 而非加列）。

**幂等性**：降级反向（DROP 新 CK + ADD 旧 CK）。`action` 列已是 VARCHAR(32)，
`REUSE`（5 字符）无需改列宽。

**两库同步**：prod + test（迁移链 0147 → 0148）。

Revision ID: 0148
"""

from __future__ import annotations

from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


_NEW_CK = (
    "action IN ('CREATE','UPDATE','DELETE','RESTORE','REUSE',"
    "'auth.login','auth.login_failed','auth.logout',"
    "'auth.password_changed','user.password_reset')"
)
_OLD_CK = (
    "action IN ('CREATE','UPDATE','DELETE','RESTORE',"
    "'auth.login','auth.login_failed','auth.logout',"
    "'auth.password_changed','user.password_reset')"
)


def upgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_action", "audit_log", _NEW_CK)


def downgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_action", "audit_log", _OLD_CK)
