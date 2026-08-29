"""add_oracle_version - 数据源表增加 Oracle 版本字段，用于 NL2SQL 分页语法判断。

Oracle 11g 用 ROWNUM，12c+ 用 FETCH FIRST N ROWS ONLY。
"""

from alembic import op
from sqlalchemy import Column, String

revision = "0007_add_oracle_version"
down_revision = "0006_session_query_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("data_source", Column("oracle_version", String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("data_source", "oracle_version")
