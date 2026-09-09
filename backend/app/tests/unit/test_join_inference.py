"""join_inference 纯函数测试：Sage X3（THBI）列名约定关联推断。

纯函数（不触 DB）：只依赖 TableSchemaRead 构造 fake schema，验证
infer_sage_x3_name_convention_joins 的推断规则与边界。
"""

from __future__ import annotations

from app.domain.schemas import ColumnSchemaRead, TableSchemaRead
from app.services.join_inference import infer_sage_x3_name_convention_joins


def _col(name: str) -> ColumnSchemaRead:
    return ColumnSchemaRead(column_name=name, data_type="VARCHAR2", nullable=True)


def _table(name: str, columns: list[str]) -> TableSchemaRead:
    return TableSchemaRead(
        table_name=name,
        columns=[_col(c) for c in columns],
        primary_keys=[],
        foreign_keys=[],
    )


def test_infers_master_and_header_references_within_selected_set():
    """选中表集内，引用列（全库同名）命中注册表 → 生成到主数据/单据的 join。"""
    tables = [
        _table("ITMMASTER", ["ITMREF_0", "ITMDES1_0"]),
        _table("PORDER", ["POHNUM_0", "BPCNUM_0", "ORDDAT_0"]),
        _table("PORDERQ", ["POHNUM_0", "POPLIN_0", "ITMREF_0", "QTYUOM_0"]),
        _table("BPCUSTOMER", ["BPCNUM_0", "BPCNAM_0"]),
    ]

    joins = infer_sage_x3_name_convention_joins(tables)

    keyed = {(j.source_table, j.target_table, j.source_columns[0]): j for j in joins}
    assert ("PORDERQ", "ITMMASTER", "ITMREF_0") in keyed  # 明细 → 物料主数据
    assert ("PORDER", "BPCUSTOMER", "BPCNUM_0") in keyed   # 单据 → 客户主数据
    assert ("PORDERQ", "PORDER", "POHNUM_0") in keyed      # 明细 → 单据头
    for j in joins:
        assert j.inferred_by == "name_convention"
        assert j.relation_type == "foreign_key"


def test_skips_self_reference_of_table_own_key():
    """表自身携带其主键列（如 ITMMASTER.ITMREF_0）→ 不生成自引用 join。"""
    tables = [_table("ITMMASTER", ["ITMREF_0", "ITMDES1_0"])]

    joins = infer_sage_x3_name_convention_joins(tables)

    assert joins == []


def test_no_join_when_referenced_target_not_in_selected_set():
    """引用目标表不在导入表集内（本次未选）→ 不生成悬空目标 join。"""
    tables = [_table("PORDERQ", ["POHNUM_0", "POPLIN_0", "ITMREF_0"])]

    joins = infer_sage_x3_name_convention_joins(tables)

    assert joins == []


def test_returns_empty_for_non_sage_schema():
    """无 Sage X3 约定列（如 PG/MySQL 小写列名）→ 空推断，不误伤普通库。"""
    tables = [
        _table("orders", ["id", "customer_id"]),
        _table("customers", ["id", "name"]),
    ]

    joins = infer_sage_x3_name_convention_joins(tables)

    assert joins == []


def test_lowercase_column_names_do_not_match_registry():
    """注册表按大写匹配：小写业务库列名不应被误判为 Sage X3 引用。"""
    tables = [
        _table("PORDER", ["pohnum_0"]),
        _table("porder", ["pohnum_0"]),
    ]

    joins = infer_sage_x3_name_convention_joins(tables)

    assert joins == []


def test_multiple_reference_columns_each_produce_distinct_join():
    """单表内多个引用列（不同目标）→ 各生成一条 join，互不合并、不重复。"""
    tables = [
        _table("PRECEIPTD", ["PTHNUM_0", "PTDLIN_0", "ITMREF_0"]),
        _table("PRECEIPT", ["PTHNUM_0"]),
        _table("ITMMASTER", ["ITMREF_0"]),
    ]

    joins = infer_sage_x3_name_convention_joins(tables)

    pairs = {(j.source_table, j.source_columns[0], j.target_table) for j in joins}
    assert ("PRECEIPTD", "PTHNUM_0", "PRECEIPT") in pairs
    assert ("PRECEIPTD", "ITMREF_0", "ITMMASTER") in pairs
    assert len(joins) == len(
        {
            (j.source_table, tuple(j.source_columns), j.target_table, tuple(j.target_columns))
            for j in joins
        }
    )
