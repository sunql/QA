"""`metadata_snapshot` 库级不可变触发器（WP-ONT-AI A1，0147）。

安全审查 M-3：`metadata_snapshot` 是「证据」——内容寻址、只增不改。应用层已
保证不 UPDATE / DELETE，但**库层**才是最后一道闸：误操作、DBA 直连、或将来
某个新写的代码路径都可能绕过服务层纪律。没有触发器时，「不可变」只是一句
注释；有了触发器，它是一条**数据库会拒绝执行**的约束。

参照 `0025_audit_immutability.py` 的做法（同一套防御性深度）：

- `BEFORE UPDATE OR DELETE` 行级触发器 ⇒ 应用层 UPDATE / DELETE 直接报错；
- `DROP TRIGGER` / `DROP FUNCTION` 用 `IF EXISTS` ⇒ 幂等降级（万一被手动删过）。

为什么单独开一个 revision（而不是原地改 0146）：0146 已在评审期之外推进，
本仓的迁移链一旦有库 stamp 过就不该原地改；新开 revision 保证「已到 0146 的
库」也能升上来。

⚠️ **TRUNCATE 不受影响**：行级 BEFORE 触发器不拦 TRUNCATE ⇒ 测试夹具的
「每测试清库」照常工作（`app/tests/_pg_support.py::_truncateAll`）。
"""

from __future__ import annotations

from alembic import op

revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None

_TABLE_SNAPSHOT = "metadata_snapshot"


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_snapshot_modification()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'metadata_snapshot 是不可变证据表，禁止 UPDATE / DELETE（内容寻址 + 只增不改）';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS tr_metadata_snapshot_immutable ON metadata_snapshot;"
    )
    op.execute(
        "CREATE TRIGGER tr_metadata_snapshot_immutable "
        "BEFORE UPDATE OR DELETE ON metadata_snapshot "
        "FOR EACH ROW EXECUTE FUNCTION prevent_snapshot_modification();"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS tr_metadata_snapshot_immutable ON metadata_snapshot;"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_snapshot_modification();")
