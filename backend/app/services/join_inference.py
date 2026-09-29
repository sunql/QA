"""Sage X3（THBI）列名约定的关联关系自动推断。

业务库（Oracle THBI）不声明任何外键约束（schema 缓存 primary_keys / foreign_keys
均为空），通用「声明外键」推断拿不到边。Sage X3 采用系统性命名约定：被引用的
主数据/单据主键列在整库以同一列名出现——例如 ITMREF_0（物料）指向 ITMMASTER，
POHNUM_0（采购订单号）指向 PORDER，BPRNUM_0（业务伙伴）指向 BPARTNER。

本模块维护这一约定的最小注册表（条目源自 seed_ontology PROPERTIES 的主键真值，
键列名在业务对象间全库唯一），对「本次导入的表集」产出 deterministic 的
ProposedJoin 候选，供导入预览勾选后落库。

设计约束（避免误导候选）：
- 仅推断两端都在给定表集内的边（避免悬空目标类）；
- 跳过源表 == 目标表（表自带其主键列，如 ITMMASTER.ITMREF_0，不产生自引用）；
- 注册表键按大写匹配：小写命名的 PG/MySQL 库不受影响；
- 不做跨文档复合键的语义拼接（如 PORDERQ.ITMREF_0 → PPRICLIST.PLICRI2_0），
  此类业务关联交给「关联关系重构」页人工维护。

输入不可变：返回新列表，不改动 tables。
"""

from __future__ import annotations

from app.domain.schemas import ProposedJoin, TableSchemaRead

# fk 列名（大写） -> (目标 source_table, 目标键列)。
# 键列名要求在整个业务对象集内唯一指向一个目标表；有歧义的列名（如 NUM_0 同时是
# PINVOICE 与 PAYMENTH 的主键）不收入注册表，避免跨单据误连。
SAGE_X3_REFERENCE_MAP: dict[str, tuple[str, str]] = {
    # 主数据引用
    "ITMREF_0": ("ITMMASTER", "ITMREF_0"),     # 物料
    "BPRNUM_0": ("BPARTNER", "BPRNUM_0"),      # 业务伙伴
    "BPCNUM_0": ("BPCUSTOMER", "BPCNUM_0"),    # 客户
    "BPSNUM_0": ("BPSUPPLIER", "BPSNUM_0"),    # 供应商
    "BCRNUM_0": ("BPCARRIER", "BCRNUM_0"),    # 承运人
    "FCY_0": ("FACILITY", "FCY_0"),            # 地点/工厂
    # 单据头引用
    "PTHNUM_0": ("PRECEIPT", "PTHNUM_0"),      # 收货单
    "POHNUM_0": ("PORDER", "POHNUM_0"),        # 采购订单
    "PQHNUM_0": ("PQUOTAT", "PQHNUM_0"),       # 采购报价
    "YPTHNUM_0": ("YPRECEIPT", "YPTHNUM_0"),   # 到货/收货单（Y 扩展）
}


def infer_sage_x3_name_convention_joins(tables: list[TableSchemaRead]) -> list[ProposedJoin]:
    """对给定表集推断 Sage X3 列名约定关联（两端都在表集内的边）。

    Args:
        tables: 本次要导入的表（已过滤/白名单收窄后的子集）。

    Returns:
        新的 ProposedJoin 候选列表；无匹配时为空。每条 relation_type='foreign_key'，
        inferred_by='name_convention'，供前端预览标注来源。
    """
    if not tables:
        return []
    table_by_upper = {t.table_name.upper(): t for t in tables}

    joins: list[ProposedJoin] = []
    seen: set[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = set()
    for source in tables:
        source_upper = source.table_name.upper()
        for col in source.columns:
            ref = SAGE_X3_REFERENCE_MAP.get(col.column_name.upper())
            if ref is None:
                continue
            target_table, target_key = ref
            target = table_by_upper.get(target_table.upper())
            # 目标表不在本次导入集内：不生成悬空边
            if target is None:
                continue
            # 源表即目标表（自带主键列）：不生成自引用边
            if source_upper == target_table.upper():
                continue
            dedupe_key = (
                source_upper,
                (col.column_name.upper(),),
                target_table.upper(),
                (target_key.upper(),),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            joins.append(
                ProposedJoin(
                    source_table=source.table_name,
                    source_columns=[col.column_name],
                    target_table=target.table_name,
                    target_columns=[target_key],
                    join_type="INNER",
                    relation_type="foreign_key",
                    is_selected=True,
                    inferred_by="name_convention",
                )
            )
    return joins
