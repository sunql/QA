"""校准采购域 5 个本体类的 source_table：DWD_* 词表 → ERP 业务表词表（0153）。

**触发**：A1「本地栈同构核对」（2026-11-18）实测到三条**互相独立、互相印证**的证据链 ——
迁移 ``0039``/``0040`` 把采购域 5 个类的 ``source_table`` 绑到了 **DWD_* 词表**
（``DWD_SUPPLIER`` / ``DWD_MATERIAL`` / ``DWD_PURCHASE_ORDER`` /
``DWD_GOODS_RECEIPT`` / ``DWD_INCOMING_INSPECTION``），而：

1. **这些表在系统的查询目标里不存在**。本体 SSOT（``backend/seed_ontology.py``）
   把采购域 30 个类绑在 **ERP 业务表** 上（Supplier→``BPSUPPLIER``、
   ItemMaster→``ITMMASTER``、PurchaseOrder→``PORDER``、Receipt→``PRECEIPT``），
   ``PROPERTIES`` 也按同一批表名组织属性列（``BPSNUM`` / ``ITMREF_0`` /
   ``ORDERQTY`` …）⇒ 类名说「表 = DWD_SUPPLIER」、属性列却是 ERP 列 ⇒
   拼出来的 SQL 指向一张**不存在的表**。
   （实测本地栈只有 ``BPSUPPLIER`` / ``PORDER`` / ``PORDERQ`` 三张真实 ERP 表，
   无任何 ``DWD_*`` 表。）

2. **它让 ``seed_ontology.seed()`` 在干净库上直接崩**。
   ``uq_ontology_class_name_version`` = ``UNIQUE(class_name, version)``；
   ``_seedClasses`` 按 **source_table** 查重 ⇒ 迁移已占用 ``class_name='Supplier'``
   ⇒ seed 查不到 ``BPSUPPLIER`` ⇒ 插入同名类 ⇒ 唯一约束违例（已实测复现）。

3. **它与测试夹具的既定契约相反**。
   ``app/tests/_pg_support.seedHeaderOntologyClasses`` 明确「**必须带
   seed_ontology.CLASSES 同款 source_table**」（否则后续 seed 撞唯一约束）
   ⇒ 两个种子在同一个 ``class_name`` 上写**不同**的 ``source_table``，
   谁先跑谁说了算 ⇒ 用例 verdict 取决于夹具顺序（实测：
   0039/0040 的两个重放用例直接红）。

**变更**：**仅 UPDATE 数据**（不改表结构 ⇒ ``alembic check`` 与
``scripts/check_schema_drift.py`` 均不受影响）。按 ``(class_name, version=1, 旧 DWD 值)``
**三元组**匹配 ⇒ 只改「迁移 0039/0040 留下的那一行」，已经是规范值的行**不动**
（幂等；也对「别的路径写入的同名类 / 后续版本」保持保守）。

``IncomingInspection``：``seed_ontology.CLASSES`` 里**没有**这个类
（来料检验尚无物理表）⇒ 规范值是 ``NULL``（"未绑定物理表"），
与 ``seedHeaderOntologyClasses`` 对它的处理（置 ``None``）一致 ⇒ 两侧不再分叉。

**不碰 description / class_alias**：那是**内容**，归 ``seed_ontology`` 管
（本迁移只管**绑定**）。``seed_ontology`` 侧的回写由 ``0153`` 同批改动补齐。

**降级**：反向 UPDATE 回 DWD_* 词表（回到 0039/0040 的口径）。

**两库同步**：prod + test（迁移链 0152 → 0153）。

Revision ID: 0153
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None


# (class_name, 旧 DWD 词表值, 规范 ERP 词表值)
# 规范值取自 backend/seed_ontology.py 的 CLASSES（本体 SSOT），
# IncomingInspection 不在 CLASSES 里 ⇒ 规范值为 NULL（未绑定物理表）。
_RECONCILE_TO_BUSINESS_TABLE = (
    ("Supplier", "DWD_SUPPLIER", "BPSUPPLIER"),
    ("ItemMaster", "DWD_MATERIAL", "ITMMASTER"),
    ("PurchaseOrder", "DWD_PURCHASE_ORDER", "PORDER"),
    ("Receipt", "DWD_GOODS_RECEIPT", "PRECEIPT"),
    ("IncomingInspection", "DWD_INCOMING_INSPECTION", None),
)

# `version = 1` 是**必须的锚**：`uq_ontology_class_name_version` 允许多版本共存，
# 而 0039/0040 落的是 `(class_name, version=1)`。不锚版本 ⇒ 将来某个 v2 类若恰好
# 还带着旧 DWD 词表，会被本迁移一并改掉（越权改后续版本的绑定）。
_UPDATE_SQL = """
    UPDATE ontology_class
       SET source_table = :canonical,
           updated_time = now()
     WHERE class_name = :class_name
       AND source_table = :legacy
       AND version = 1
"""


def upgrade() -> None:
    """把 5 个采购域类的 source_table 校准到 ERP 业务表词表。"""
    bind = op.get_bind()
    for className, legacy, canonical in _RECONCILE_TO_BUSINESS_TABLE:
        bind.execute(
            text(_UPDATE_SQL),
            {"class_name": className, "legacy": legacy, "canonical": canonical},
        )


def downgrade() -> None:
    """反向：校准回 0039/0040 的 DWD_* 词表。"""
    bind = op.get_bind()
    for className, legacy, canonical in _RECONCILE_TO_BUSINESS_TABLE:
        if canonical is None:
            # NULL → DWD 词表：不能用 `source_table = NULL` 等值匹配，
            # 改用「IS NULL」守卫，避免把已被别的路径绑好的行也改掉。
            bind.execute(
                text(
                    """
                    UPDATE ontology_class
                       SET source_table = :legacy,
                           updated_time = now()
                     WHERE class_name = :class_name
                       AND source_table IS NULL
                       AND version = 1
                    """
                ),
                {"class_name": className, "legacy": legacy},
            )
            continue
        bind.execute(
            text(_UPDATE_SQL),
            {"class_name": className, "legacy": canonical, "canonical": legacy},
        )
