"""audit_log + kpi_catalog_history immutability triggers（Phase 4.5 follow-up）。

Phase 4.5 0024 migration 创建了两张不可变日志表。应用层只 session.add，
但 DB 层若有 DBA / 误操作 / SQL 注入风险，仍可能 UPDATE 或 DELETE。
加 PostgreSQL trigger 作为 defense-in-depth：禁止对这两张表的 UPDATE / DELETE。

DROP TRIGGER + DROP FUNCTION 用 IF EXISTS：幂等降级（万一 trigger 被手动删了）。

安全审查反馈（security-reviewer）：审计完整性不应只靠应用层。
"""

from __future__ import annotations

from alembic import op

revision = "0025_audit_immutability"
down_revision = "0024_governance_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 通用阻止函数：
    # - DELETE 一律禁止
    # - UPDATE 仅当不是级联 FK 操作时禁止（pg_trigger_depth > 0 表示由外层 trigger / cascade 触发）
    # 例外场景：kpi_catalog_history.kpi_id ON DELETE SET NULL — 删除 KPI 时会触发
    # UPDATE kpi_catalog_history SET kpi_id=NULL ...，这是治理要求（历史保留），必须放行。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_modification()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'audit_log / kpi_catalog_history 是不可变表，禁止 DELETE（治理前提）';
            END IF;
            -- TG_OP = 'UPDATE'
            IF pg_trigger_depth() = 0 THEN
                RAISE EXCEPTION 'audit_log / kpi_catalog_history 是不可变表，禁止应用层 UPDATE（治理前提）';
            END IF;
            -- 级联 FK 操作（pg_trigger_depth > 0）：放行
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # audit_log 触发器（每条 op.execute 一条 SQL，asyncpg 不允许多语句 prepared statement）
    op.execute("DROP TRIGGER IF EXISTS tr_audit_log_immutable ON audit_log;")
    op.execute(
        "CREATE TRIGGER tr_audit_log_immutable "
        "BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();"
    )
    # kpi_catalog_history 触发器
    op.execute("DROP TRIGGER IF EXISTS tr_kpi_history_immutable ON kpi_catalog_history;")
    op.execute(
        "CREATE TRIGGER tr_kpi_history_immutable "
        "BEFORE UPDATE OR DELETE ON kpi_catalog_history "
        "FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tr_audit_log_immutable ON audit_log;")
    op.execute("DROP TRIGGER IF EXISTS tr_kpi_history_immutable ON kpi_catalog_history;")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_modification();")
