"""采购本体对象核对与补齐（0158，M1-1.4）。

**做什么**
----------
把 V0.3 §3.2 的 10 个采购本体对象逐条对现状后的**补齐动作**落库：

1. **复活 7 个墓碑类**（``ontology_class.valid_to`` 清空）；
2. **新增 4 个业务对象**（``business_object``：PR / QUOT / POL / PAY）；
3. **改指 1 个业务对象**（INVOICE 的头类：DWD_AP_INVOICE_DTL → DWD_PURCHASE_INVOICE）。

**为什么必须做（口径不丢）**
--------------------------
``ontology_metric_source`` 用 **class_id** 引用本体类；编译器遇到**墓碑类**的源引用
会**整条丢弃**（``semantic_compiler_sections.groupMetricSources``）⇒ 指标只剩一半口径
（OTD 只剩分母、三单匹配只剩 FILTER）⇒ **静默口径漂移**，且采购域切片里
**一个采购 KPI 都到不了**（指标按类锚定，类没了 ⇒ 指标归不了属 ⇒ fail-closed 排除）。

而「复活」必须**原地**做（清 ``valid_to``）：若改成新建 v2 行，``class_id`` 会变，
指标源仍指向旧 id ⇒ 照样被丢弃（换汤不换药）。

**变更日志表（为什么不是「纯 DML」）**
----------------------------------
本迁移创建一个**只服务于本迁移**的表
``procurement_object_reconciliation_journal``，在升级时逐条记录**实际改了什么**：

- ``revive``：类的 id + **原墓碑时间戳**；
- ``insert_bo``：新 BO 的 **code**（``business_object`` 的主键就是 code，
  降级只删**这些** code）；
- ``repoint_bo``：BO 的**原头类 id** + **原 graph_label**。

没有它，降级只能按「类名 + 当前状态」反推 ⇒ 会把**升级前就已活跃**的同名类
一并墓碑化（误改不属于本迁移的数据），并且只能把原 ``graph_label`` /
墓碑时间戳**硬编码猜回去**。降级末尾删除该表 ⇒ 降级后库形状与本迁移无关。

**为什么是「自包含」的**
----------------------
本迁移**不 import ``app.*``**（仓内迁移风格：0154/0155/0156/0157 同款）⇒
迁移在**任何**环境都能跑（含生产）。与仓内决策目录
``app/domain/procurement_object_catalog.py`` 的一致性由**单测里的 AST 漂移守卫**钉住
（``app/tests/unit/test_procurement_object_catalog.py``）—— 不用运行时 import
（运行时 import 会让迁移依赖应用代码 ⇒ 生产回滚时可能 ImportError）。

**为什么在 Lineage A 环境是 no-op**
---------------------------------
所有锚点都是 **类名**（Lineage B 词表：``DIM_SUPPLIER`` / ``DWD_*``）。
测试库 5434 是 Lineage A（``Supplier`` / ``ItemMaster`` …）⇒ 找不到类 ⇒
**业务数据整批不改**（不报错、不产生半截状态）；只多一张**空的**日志表。

**幂等**
------
每个动作都有存在性守卫：已复活的类不再改、已存在的 BO 不重复插、已改指的 BO 不再改
⇒ 重复执行结果相同（第二遍 journal 也不新增行）。

**降级（与升级对称）**
--------------------
反向四步，**全部以 journal 为准**：

1. 改指回原头类，并把 ``graph_label`` 还原成 journal 里的**原值**；
2. 只删 journal 里记录的那 4 个 BO code，且**仅当无任何下游引用**
   （``pg_constraint`` 里**动态枚举**引用 ``business_object`` 的外键；
   任一外键有引用 ⇒ 保留该行，宁可留一行也不让降级失败）；
3. 按 journal 的 id 恢复 7 个类的**原墓碑时间戳**（不是「执行时刻」）；
4. 删除 journal 表。

Revision ID: 0158
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text

from alembic import op

revision = "0158"
down_revision = "0157"
branch_labels = None
depends_on = None

CREATED_BY = "m1-1.4-migration"

_JOURNAL_TABLE = "procurement_object_reconciliation_journal"

# ---------------------------------------------------------------------------
# 决策数据（与 app/domain/procurement_object_catalog.py 一一对应；
# 一致性由单测的 AST 漂移守卫钉住 —— 改这里必须同步改那里）
# ---------------------------------------------------------------------------

_REVIVE_CLASS_NAMES: tuple[str, ...] = (
    "DWD_GOODS_RECEIPT_LINE",
    "DWD_PURCHASE_INVOICE",
    "DWD_PURCHASE_INVOICE_LINE",
    "DWD_PURCHASE_ORDER_LINE",
    "DWD_PURCHASE_REQUISITION_LINE",
    "DWD_REQUISITION_ORDER_LINK",
    "DWD_SUPPLIER_PAYMENT",
)

# (code, name, header_class, graph_label, description)
_NEW_BUSINESS_OBJECTS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "PR",
        "采购申请",
        "DWD_PURCHASE_REQUISITION_LINE",
        "DWD_PURCHASE_REQUISITION_LINE",
        "内部采购需求申请（请购单行）",
    ),
    (
        "QUOT",
        "报价",
        "DWD_SUPPLIER_PRICE_LIST_HEADER",
        "DWD_SUPPLIER_PRICE_LIST_HEADER",
        "供应商提供的报价（报价清单头）",
    ),
    (
        "POL",
        "采购订单行",
        "DWD_PURCHASE_ORDER_LINE",
        "DWD_PURCHASE_ORDER_LINE",
        "采购订单中的具体物料明细（采购 KPI 的口径锚点）",
    ),
    (
        "PAY",
        "采购付款",
        "DWD_SUPPLIER_PAYMENT",
        "DWD_SUPPLIER_PAYMENT",
        "对供应商的付款交易",
    ),
)

# (bo_code, from_class, to_class)
_REPOINTED_BUSINESS_OBJECTS: tuple[tuple[str, str, str], ...] = (
    ("INVOICE", "DWD_AP_INVOICE_DTL", "DWD_PURCHASE_INVOICE"),
)

_DOMAIN_CODE = "PROCUREMENT"


# ---------------------------------------------------------------------------
# SQL（全部防御式：按「类名 + 存在性」锚定，不按 id ⇒ 换环境不炸）
# ---------------------------------------------------------------------------
#
# ⚠️ 形态约束：**SQL 里不出现 `:name` 以外的参数语法**。
# 迁移经 SQLAlchemy ``text()`` 执行 ⇒ 只认 ``:name`` 具名参数，``$1`` 会被原样
# 透传（plpgsql 的 ``DO`` 块里只有 ``EXECUTE ... USING`` 才允许用它）。

_CREATE_JOURNAL_SQL = f"""
    CREATE TABLE IF NOT EXISTS {_JOURNAL_TABLE} (
        id bigserial PRIMARY KEY,
        kind varchar(20) NOT NULL,
        subject varchar(100) NOT NULL,
        class_id bigint,
        old_valid_to timestamptz,
        old_header_class_id bigint,
        old_graph_label varchar(100),
        created_time timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_procurement_reconciliation_kind
            CHECK (kind IN ('revive', 'insert_bo', 'repoint_bo'))
    )
"""

# 复活：**不按 version 过滤**（墓碑行可能不是 v1；按版本锚定会静默 0 行 =
# 类仍是墓碑 = 指标源照旧被丢弃，而测试环境恰好种的是 v1 ⇒ 分支永不暴露）。
# 先拍快照再更新，把「原墓碑时间戳」落 journal（降级时可精确还原）。
_REVIVE_SQL = f"""
    WITH old AS (
        SELECT c.id, c.valid_to
          FROM ontology_class c
         WHERE c.class_name = CAST(:class_name AS varchar(100))
           AND c.valid_to IS NOT NULL
    ), upd AS (
        UPDATE ontology_class c
           SET valid_to = NULL,
               updated_time = now()
          FROM old o
         WHERE c.id = o.id
        RETURNING c.id
    )
    INSERT INTO {_JOURNAL_TABLE} (kind, subject, class_id, old_valid_to)
    SELECT 'revive', CAST(:class_name AS varchar(100)), o.id, o.valid_to
      FROM old o
      JOIN upd u ON u.id = o.id
"""

# ⚠️ 所有参数都**显式 CAST**：asyncpg 会按参数在语句里的用法推断类型，
# 同一个参数既出现在 SELECT 输出（推断 text）又出现在等值比较（推断 varchar）
# ⇒ `AmbiguousParameterError: inconsistent types deduced for parameter $1`
# ⇒ 迁移整体失败（不是「某个分支不生效」）。
_INSERT_BO_SQL = f"""
    WITH inserted AS (
        INSERT INTO business_object (
            code, name, header_class_id, primary_domain_id, graph_label,
            description, created_by, created_time, updated_time
        )
        SELECT CAST(:code AS varchar(20)), CAST(:name AS varchar(100)), c.id,
               CAST(:domain_code AS varchar(20)), CAST(:graph_label AS varchar(100)),
               CAST(:description AS text), CAST(:created_by AS varchar(50)), now(), now()
          FROM (
                SELECT id
                  FROM ontology_class
                 WHERE class_name = CAST(:header_class AS varchar(100))
                   AND valid_to IS NULL
                 ORDER BY version DESC, id DESC
                 LIMIT 1
               ) AS c
         WHERE NOT EXISTS (
                   SELECT 1 FROM business_object b WHERE b.code = CAST(:code AS varchar(20))
               )
           AND NOT EXISTS (SELECT 1 FROM business_object b2 WHERE b2.header_class_id = c.id)
        RETURNING code, header_class_id
    )
    INSERT INTO {_JOURNAL_TABLE} (kind, subject, class_id)
    SELECT 'insert_bo', code, header_class_id FROM inserted
"""

# 改指：把「原头类 id + 原 graph_label」先拍快照，再更新并落 journal。
_REPOINT_BO_SQL = f"""
    WITH old AS (
        SELECT b.code, b.header_class_id, b.graph_label
          FROM business_object b
         WHERE b.code = CAST(:bo_code AS varchar(20))
           AND b.header_class_id = (
                SELECT id
                  FROM ontology_class
                 WHERE class_name = CAST(:from_class AS varchar(100))
                 ORDER BY version DESC, id DESC
                 LIMIT 1
               )
    ), target AS (
        SELECT id
          FROM ontology_class
         WHERE class_name = CAST(:to_class AS varchar(100))
           AND valid_to IS NULL
         ORDER BY version DESC, id DESC
         LIMIT 1
    ), upd AS (
        UPDATE business_object b
           SET header_class_id = t.id,
               graph_label = CAST(:to_class AS varchar(100)),
               updated_time = now()
          FROM old o, target t
         WHERE b.code = o.code
           AND NOT EXISTS (
                SELECT 1 FROM business_object x
                 WHERE x.header_class_id = t.id
                   AND x.code <> b.code
               )
        RETURNING b.code, t.id AS new_header_class_id
    )
    INSERT INTO {_JOURNAL_TABLE}
        (kind, subject, class_id, old_header_class_id, old_graph_label)
    SELECT 'repoint_bo', CAST(:bo_code AS varchar(20)), u.new_header_class_id,
           o.header_class_id, o.graph_label
      FROM old o
      JOIN upd u ON u.code = o.code
"""

# 降级①：改指回原头类（原值来自 journal；只改「仍是本迁移改指后的那个类」的行）。
_REPOINT_BACK_SQL = f"""
    UPDATE business_object b
       SET header_class_id = j.old_header_class_id,
           graph_label = j.old_graph_label,
           updated_time = now()
      FROM {_JOURNAL_TABLE} j
     WHERE j.kind = 'repoint_bo'
       AND j.subject = b.code
       AND b.header_class_id = j.class_id
"""

# 降级②：删掉本迁移新增的 BO —— 只认 journal 记录的那几个 code
# （``business_object`` 的**主键是 code，没有 id 列**），
# 并**动态枚举** ``pg_constraint`` 里所有引用 ``business_object`` 的外键：
# 任一外键存在引用行 ⇒ 保留该行（宁可留一行，也不让降级撞 RESTRICT 失败）。
# ⚠️ 本 DO 块**不含具名参数**（参数必须由 SQLAlchemy 替换，而 ``DO`` 体是字符串
# 字面量 ⇒ 放不进去）；目标集合来自 journal，本就无需参数。
_DELETE_BO_SQL = """
    DO $migration$
    DECLARE
        target record;
        ref record;
        blocked boolean;
    BEGIN
        FOR target IN
            SELECT j.subject
              FROM procurement_object_reconciliation_journal j
             WHERE j.kind = 'insert_bo'
             ORDER BY j.id DESC
        LOOP
            blocked := false;
            FOR ref IN
                SELECT con.conrelid::regclass AS tbl,
                       att.attname AS col,
                       (SELECT a2.attname
                          FROM pg_attribute a2
                         WHERE a2.attrelid = con.confrelid
                           AND a2.attnum = con.confkey[1]) AS ref_col
                  FROM pg_constraint con
                  JOIN pg_attribute att
                    ON att.attrelid = con.conrelid
                   AND att.attnum = ANY (con.conkey)
                 WHERE con.contype = 'f'
                   AND con.confrelid = 'business_object'::regclass
            LOOP
                IF ref.ref_col IS DISTINCT FROM 'code' THEN
                    -- 不认识的外键形态 ⇒ 保守：视为有引用
                    blocked := true;
                ELSE
                    EXECUTE format(
                        'SELECT EXISTS (SELECT 1 FROM %s WHERE %I = $1)',
                        ref.tbl, ref.col
                    ) INTO blocked USING target.subject;
                END IF;
                EXIT WHEN blocked;
            END LOOP;
            IF NOT blocked THEN
                DELETE FROM business_object WHERE code = target.subject;
            END IF;
        END LOOP;
    END
    $migration$
"""

# 降级③：按 journal 的 id 恢复原墓碑时间戳（不是「执行时刻」）。
_RETOMBSTONE_SQL = f"""
    UPDATE ontology_class c
       SET valid_to = j.old_valid_to,
           updated_time = now()
      FROM {_JOURNAL_TABLE} j
     WHERE j.kind = 'revive'
       AND c.id = j.class_id
       AND c.valid_to IS NULL
"""

# 降级④：删掉日志表 ⇒ 降级后库形状与本迁移无关。
_DROP_JOURNAL_SQL = f"DROP TABLE IF EXISTS {_JOURNAL_TABLE}"


class _Executor(Protocol):
    """能执行 SQL 的对象（``Connection`` / ``AsyncSession`` 都满足）。"""

    def execute(self, statement: Any, parameters: Any = None) -> Any: ...


def reconciliationStatements() -> tuple[tuple[str, dict[str, Any]], ...]:
    """返回「建日志表 + 复活 + 补 BO + 改指」的 (SQL, 参数) 序列（**纯函数**）。

    为什么把它抽出来：alembic 的 ``op.get_bind()`` 是**同步** Connection，
    而集成测试跑在**异步** Session 上 ⇒ 两条路径的执行器不同。
    抽成「语句序列」后，两条路径消费**同一份 SQL** ⇒ 不会各写一份而漂移。
    """
    steps: list[tuple[str, dict[str, Any]]] = [(_CREATE_JOURNAL_SQL, {})]
    for className in _REVIVE_CLASS_NAMES:
        steps.append((_REVIVE_SQL, {"class_name": className}))
    for code, name, headerClass, graphLabel, description in _NEW_BUSINESS_OBJECTS:
        steps.append(
            (
                _INSERT_BO_SQL,
                {
                    "code": code,
                    "name": name,
                    "header_class": headerClass,
                    "domain_code": _DOMAIN_CODE,
                    "graph_label": graphLabel,
                    "description": description,
                    "created_by": CREATED_BY,
                },
            )
        )
    for boCode, fromClass, toClass in _REPOINTED_BUSINESS_OBJECTS:
        steps.append(
            (
                _REPOINT_BO_SQL,
                {"bo_code": boCode, "from_class": fromClass, "to_class": toClass},
            )
        )
    return tuple(steps)


def revertStatements() -> tuple[tuple[str, dict[str, Any]], ...]:
    """返回反向四步的 (SQL, 参数) 序列（纯函数，同上）。"""
    return (
        (_REPOINT_BACK_SQL, {}),
        (_DELETE_BO_SQL, {}),
        (_RETOMBSTONE_SQL, {}),
        (_DROP_JOURNAL_SQL, {}),
    )


def applyReconciliation(bind: _Executor) -> None:
    """执行「建日志表 + 复活 + 补 BO + 改指」（同步执行器；alembic 用这个）。"""
    for sql, params in reconciliationStatements():
        bind.execute(text(sql), params)


def revertReconciliation(bind: _Executor) -> None:
    """反向四步（改指回原头类 → 删新增 BO → 恢复原墓碑 → 删日志表）。"""
    for sql, params in revertStatements():
        bind.execute(text(sql), params)


def upgrade() -> None:
    """复活 7 个墓碑类 + 新增 4 个业务对象 + 改指 INVOICE 的头类。"""
    applyReconciliation(op.get_bind())


def downgrade() -> None:
    """反向四步（以 journal 为准，精确还原）。"""
    revertReconciliation(op.get_bind())
