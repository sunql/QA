"""Phase 3.3 缺失业务对象建模单元测试（RED）。

验证 seed_ontology 新增的 6 个采购域业务对象类（采购报价/采购发票/付款，
各带明细）的契约：

- CLASSES 注册：类名/中文别名/source_table 三要素
- PROPERTIES 主键布局：表头单主键、明细复合主键（单据号+行号）+ FK 指回表头
- 关键 FK：明细物料 → ITMMASTER；发票/付款供应商 → BPARTNER
- BUSINESS_JOINS：报价→请购、发票→订单/收货/付款 的复合键业务流转边
- METRICS：发票金额 / 付款金额 / 报价数量 三类 KPI

seed() 幂等由集成测试（真实 PG 5433）覆盖（`test_seed_missing_business_objects.py`，
禁止 sqlite 内存库 + 直接调用 seed，见 Harness/rules/测试规范.md）。

数据来源：ZJTH 真实库列结构（all_tab_columns 实证）。范围修正：采购域
「报价」映射 Sage X3 采购报价 PQUOTAT（非销售报价 SQUOTE）；「交付」已由
YPRECEIPT/PRECEIPT 覆盖，不再单独建模。详见 SSOT §5。
"""

from __future__ import annotations

from seed_ontology import (
    BUSINESS_JOINS,
    CLASSES,
    METRICS,
    PROPERTIES,
)

# 新对象类契约：source_table -> (class_name, class_alias)
NEW_CLASSES: dict[str, tuple[str, str]] = {
    "PQUOTAT": ("Quotation", "采购报价"),
    "PQUOTATD": ("QuotationDetail", "采购报价明细"),
    "PINVOICE": ("PurchaseInvoice", "采购发票"),
    "PINVOICED": ("PurchaseInvoiceDetail", "采购发票明细"),
    "PAYMENTH": ("Payment", "付款单"),
    "PAYMENTD": ("PaymentDetail", "付款明细"),
}

# 明细表 FK 指向：source_table -> 表头 source_table
DETAIL_PARENT: dict[str, str] = {
    "PQUOTATD": "PQUOTAT",
    "PINVOICED": "PINVOICE",
    "PAYMENTD": "PAYMENTH",
}

# 关键 FK（列名 -> 目标 source_table），跨新类必须存在
REQUIRED_FKS: dict[tuple[str, str], str] = {
    ("PQUOTATD", "ITMREF_0"): "ITMMASTER",  # 报价明细物料
    ("PQUOTATD", "PQHNUM_0"): "PQUOTAT",    # 报价明细 → 报价头
    ("PINVOICE", "BPR_0"): "BPARTNER",      # 发票供应商
    ("PINVOICED", "ITMREF_0"): "ITMMASTER",  # 发票明细物料
    ("PINVOICED", "NUM_0"): "PINVOICE",     # 发票明细 → 发票头
    ("PAYMENTH", "BPR_0"): "BPARTNER",      # 付款收款方
    ("PAYMENTD", "NUM_0"): "PAYMENTH",      # 付款明细 → 付款头
}

# 复合键业务流转边：source -> (src_cols, tgt_table, tgt_cols)
REQUIRED_JOINS: list[tuple[str, list[str], str, list[str]]] = [
    # 报价明细 → 请购明细（询价响应来源）
    ("PQUOTATD", ["PSHNUM_0", "PSDLIN_0"], "PREQUISD", ["PSHNUM_0", "PSDLIN_0"]),
    # 发票明细 → 采购订单明细（三向匹配）
    ("PINVOICED", ["POHNUM_0", "POPLIN_0"], "PORDERQ", ["POHNUM_0", "POPLIN_0"]),
    # 发票明细 → 收货明细（三向匹配）
    ("PINVOICED", ["PTHNUM_0", "PTDLIN_0"], "PRECEIPTD", ["PTHNUM_0", "PTDLIN_0"]),
    # 发票明细 → 付款明细（发票被付款核销）
    ("PINVOICED", ["PNHNUM_0", "PNDLIN_0"], "PAYMENTD", ["NUM_0", "LIN_0"]),
]

# 新 KPI：metric_name -> target_table
REQUIRED_METRICS: dict[str, str] = {
    "KPI_INVOICE_AMT": "PINVOICED",
    "KPI_PAYMENT_AMT": "PAYMENTH",
    "KPI_QUOTATION_QTY": "PQUOTATD",
}


class TestMissingObjectClasses:
    def test_new_classes_registered(self) -> None:
        """6 个新类都注册在 CLASSES，source_table 与 类名/别名匹配。"""
        registered = {c["source_table"]: c for c in CLASSES}
        assert set(NEW_CLASSES) <= set(registered)
        for table, (name, alias) in NEW_CLASSES.items():
            assert registered[table]["class_name"] == name, table
            assert registered[table]["class_alias"] == alias, table
            assert registered[table]["description"], f"{table} 缺描述"

    def test_new_classes_have_descriptive_descriptions(self) -> None:
        """描述须点明业务语义（采购发票/付款等关键词），供 LLM 理解类用途。"""
        byTable = {c["source_table"]: c["description"] for c in CLASSES}
        keywords = {
            "PQUOTAT": ("询价", "报价"),
            "PINVOICE": ("发票",),
            "PAYMENTH": ("付款",),
        }
        for table, kws in keywords.items():
            desc = byTable[table]
            assert any(k in desc for k in kws), f"{table} 描述缺少关键词: {desc}"


class TestMissingObjectProperties:
    def test_header_single_pk(self) -> None:
        """表头类恰一个主键（单据号）。"""
        for table in ("PQUOTAT", "PINVOICE", "PAYMENTH"):
            pks = [p for p in PROPERTIES[table] if p.get("pk")]
            assert len(pks) == 1, f"{table} 应单主键，实际 {len(pks)}"
            assert pks[0]["col"] in ("PQHNUM_0", "NUM_0"), table

    def test_detail_composite_pk_links_header(self) -> None:
        """明细类复合主键（单据号+行号），单据号 FK 指回表头。"""
        for table, parent in DETAIL_PARENT.items():
            pks = [p for p in PROPERTIES[table] if p.get("pk")]
            assert len(pks) == 2, f"{table} 应复合主键，实际 {len(pks)}"
            # 首列是单据号并 FK 指向表头
            assert pks[0]["fk"] == parent, f"{table} 首主键应 FK {parent}"
            assert pks[1]["name"] == "行号", f"{table} 第二主键应为行号"

    def test_key_fks_present(self) -> None:
        """关键 FK 属性必须存在且指向正确目标类。"""
        for (table, col), target in REQUIRED_FKS.items():
            props = [
                p for p in PROPERTIES[table]
                if p["col"] == col and p.get("fk") == target
            ]
            assert props, f"{table}.{col} 缺少 FK -> {target} 的属性"

    def test_amount_columns_have_business_aliases(self) -> None:
        """金额类列名或业务别名含业务词（发票金额/付款金额），供自然语言命中。

        属性名本身即可命中（如「付款金额」），别名只补同义检索词；判断覆盖
        名称 + 别名拼接文本。
        """
        aliasSpecs = {
            "PINVOICE": ("AMTNOT_0", "发票金额"),
            "PINVOICED": ("AMTNOTLIN_0", "发票金额"),
            "PAYMENTH": ("AMTCUR_0", "付款金额"),
            "PQUOTATD": ("QTYPUU_0", "询价"),
        }
        for table, (col, keyword) in aliasSpecs.items():
            prop = next(
                p for p in PROPERTIES[table]
                if p["col"] == col and not p.get("pk")
            )
            searchable = prop["name"] + "".join(prop.get("aliases") or [])
            assert keyword in searchable, (
                f"{table}.{col} 名称/别名缺少「{keyword}」: {searchable}"
            )


class TestMissingObjectJoins:
    def test_required_business_joins_present(self) -> None:
        """复合键业务流转边齐全（报价→请购、发票→订单/收货/付款）。"""
        for src, srcCols, tgt, tgtCols in REQUIRED_JOINS:
            edges = [
                j for j in BUSINESS_JOINS
                if j[0] == src and j[2] == tgt
                and j[1] == srcCols and j[3] == tgtCols
            ]
            assert edges, f"缺少 BUSINESS_JOIN {src}{srcCols} → {tgt}{tgtCols}"

    def test_invoice_payment_join_uses_real_columns(self) -> None:
        """发票→付款 join 的目标列必须指向 PAYMENTD 真实主键 NUM_0/LIN_0。"""
        edges = [
            j for j in BUSINESS_JOINS
            if j[0] == "PINVOICED" and j[2] == "PAYMENTD"
        ]
        assert edges, "缺少 PINVOICED → PAYMENTD join"
        assert edges[0][3] == ["NUM_0", "LIN_0"]


class TestMissingObjectMetrics:
    def test_new_metrics_registered(self) -> None:
        """发票/付款/报价三类 KPI 已注册且指向正确目标表。"""
        byName = {m["metric_name"]: m for m in METRICS}
        for name, target in REQUIRED_METRICS.items():
            assert name in byName, f"缺少指标 {name}"
            assert byName[name]["target_table"] == target, name


