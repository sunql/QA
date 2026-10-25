"""数据域实体化：data_domain 表 + 业务对象域绑定 + header_class_id 收紧（feat-data-domain-layer）。

方案 1（2026-10-25 用户确认设计）：
- 数据域从 agent_vocabulary.AGENT_DATA_DOMAINS 硬编码词表升级为 DB 实体，
  含主管理组织（organizations）。
- 业务对象挂域：primary_domain_id（NOT NULL，天然「有且只有一个主域」）
  + business_object_domain 附域关联表（附域 ≠ 主域由服务层校验）。
- header_class_id 收紧 NOT NULL + 唯一索引（规则①②：空绑定 BO 无存在意义；
  一个本体类只允许被一个 BO 作 header，类继承唯一主域自动成立）。

存量自检（部署前必查，per 0132 同款约定）：
  SELECT code FROM business_object WHERE header_class_id IS NULL;
  -- 命中行的三表引用（entity_mapping / feature_definition / document_entity_relation
  -- / feature_rule）必须为 0，否则本 migration 的清理守卫会 raise 阻断

dev 实测（2026-10-25，qa_metadata）：7 BO，IQC/NCR 两行 header 为空且零引用，
header_class_id 无重复绑定；organizations 存在 procurement/quality 行。

seed 三域：PROCUREMENT→org(procurement)、QUALITY→org(quality)、
LOGISTICS→NULL（dev 无对应组织行，org 子查询缺失时置 NULL，可重放）。
存量 BO 的 primary_domain_id 统一回填 PROCUREMENT（7 行均为采购域对象，
与 ontology_class 治理字段「采购域 Sheet 03 业务对象目录」同源）。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None

# BO 空绑定的引用守卫表（与 business_object_service.deleteObject 的引用检查同族；
# entity_type 是普通 varchar 非 FK，DELETE 不会触发数据库约束，必须显式守卫。
# 注意 feature_rule 的列是 data_object 不是 entity_type）。
_REF_GUARD_TABLES = (
    ("entity_mapping", "entity_type"),
    ("feature_definition", "entity_type"),
    ("document_entity_relation", "entity_type"),
    ("feature_rule", "data_object"),
)


def _refCountOrNullHeader(conn: sa.Connection, table: str, column: str) -> int:
    return conn.execute(
        sa.text(
            f"SELECT count(*) FROM {table} WHERE {column} IN "
            "(SELECT code FROM business_object WHERE header_class_id IS NULL)"
        )
    ).scalar_one()


def upgrade() -> None:
    conn = op.get_bind()

    # ---- 存量守卫：空 header 的 BO 必须零引用才允许清理 ----
    for table, column in _REF_GUARD_TABLES:
        n = _refCountOrNullHeader(conn, table, column)
        if n:
            raise RuntimeError(
                f"0133 阻断：business_object 空绑定行仍被 {table} 引用 {n} 条，"
                "请先清理引用或补绑 header_class_id 再升级"
            )

    op.create_table(
        "data_domain",
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "primary_org_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_data_domain_primary_org"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=50), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("code"),
    )

    # ---- seed 三域（幂等：ON CONFLICT DO NOTHING；org 缺失置 NULL 可重放）----
    op.execute(
        """
        INSERT INTO data_domain (code, name, primary_org_id, description, enabled, created_by)
        VALUES
            ('PROCUREMENT', '采购域',
             (SELECT id FROM organizations WHERE code = 'procurement'),
             '采购/供应商/到货等主数据与业务过程', TRUE, 'migration-0133'),
            ('QUALITY', '质量域',
             (SELECT id FROM organizations WHERE code = 'quality'),
             '来料检验/不合格处理等质量过程', TRUE, 'migration-0133'),
            ('LOGISTICS', '物流域',
             NULL,
             '仓储/运输/库存等物流过程', TRUE, 'migration-0133')
        ON CONFLICT (code) DO NOTHING
        """
    )

    # ---- 清理空绑定 BO（已过引用守卫）----
    op.execute("DELETE FROM business_object WHERE header_class_id IS NULL")

    # ---- business_object.primary_domain_id：加列 → 回填 → NOT NULL → FK ----
    op.add_column(
        "business_object",
        sa.Column("primary_domain_id", sa.String(length=20), nullable=True),
    )
    op.execute(
        "UPDATE business_object SET primary_domain_id = 'PROCUREMENT' "
        "WHERE primary_domain_id IS NULL"
    )
    op.alter_column("business_object", "primary_domain_id", nullable=False)
    op.create_foreign_key(
        "fk_business_object_primary_domain",
        "business_object", "data_domain",
        ["primary_domain_id"], ["code"],
        ondelete="RESTRICT",
    )

    # ---- 附域关联表 ----
    op.create_table(
        "business_object_domain",
        sa.Column("bo_code", sa.String(length=20), nullable=False),
        sa.Column("domain_id", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("bo_code", "domain_id"),
        sa.ForeignKeyConstraint(
            ["bo_code"], ["business_object.code"],
            name="fk_bo_domain_bo", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"], ["data_domain.code"],
            name="fk_bo_domain_domain", ondelete="CASCADE",
        ),
    )

    # ---- header_class_id 收紧：NOT NULL + 唯一索引（规则①②）----
    op.alter_column("business_object", "header_class_id", nullable=False)
    op.create_index(
        "uq_business_object_header_class",
        "business_object",
        ["header_class_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_business_object_header_class", table_name="business_object")
    op.alter_column("business_object", "header_class_id", nullable=True)
    op.drop_table("business_object_domain")
    op.drop_constraint(
        "fk_business_object_primary_domain", "business_object", type_="foreignkey"
    )
    op.drop_column("business_object", "primary_domain_id")
    # 保留一行业务数据的 DELETE 是预期（per 0052 同款约定）
    op.execute("DELETE FROM data_domain WHERE created_by = 'migration-0133'")
    op.drop_table("data_domain")
