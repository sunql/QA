"""sync_business_graph_from_thbi.buildGraphPlan 纯函数单测（不需要任何外部服务）。

覆盖：节点/边派生、编码去重、供应商名 enrichment（首个非空）、空编码跳过、
孤儿行（po_no 不在头表）跳过统计、行级 supplier_code 缺失回落头表映射。
"""

from __future__ import annotations

from scripts.sync_business_graph_from_thbi import buildGraphPlan


def _header(poNo: str, supCode: str, supName: str | None = None) -> dict:
    return {
        "po_no": poNo,
        "supplier_code": supCode,
        "supplier_name": supName,
        "order_date": "2026-01-01",
    }


def _line(poNo: str, matCode: str, supCode: str | None = None) -> dict:
    return {"po_no": poNo, "material_code": matCode, "supplier_code": supCode}


class TestBuildGraphPlan:
    def test_nodes_and_edges_derived_with_dedupe(self) -> None:
        headers = [
            _header("PO1", "S1", "甲供应商"),
            _header("PO1", "S1", "甲供应商"),  # 头表重复行 → 节点/边不重复
            _header("PO2", "S2", None),
        ]
        lines = [
            _line("PO1", "M1", "S1"),
            _line("PO1", "M2", "S1"),
            _line("PO2", "M1", "S2"),  # M1 被两家供应 → SUPPLIES 去重后 2 条
            _line("PO2", "M1", "S2"),  # 完全重复行
        ]

        plan = buildGraphPlan(headers, lines)

        assert {n["key"] for n in plan.suppliers} == {"S1", "S2"}
        assert {n["key"] for n in plan.purchaseOrders} == {"PO1", "PO2"}
        assert {n["key"] for n in plan.materials} == {"M1", "M2"}
        # PLACES：按头表 (supplier, po) 一对一
        assert {(e["fromKey"], e["toKey"]) for e in plan.places} == {
            ("S1", "PO1"),
            ("S2", "PO2"),
        }
        # CONTAINS：行去重
        assert {(e["fromKey"], e["toKey"]) for e in plan.contains} == {
            ("PO1", "M1"),
            ("PO1", "M2"),
            ("PO2", "M1"),
        }
        # SUPPLIES：distinct (supplier, material)
        assert {(e["fromKey"], e["toKey"]) for e in plan.supplies} == {
            ("S1", "M1"),
            ("S1", "M2"),
            ("S2", "M1"),
        }

    def test_supplier_name_enrichment_first_non_null_wins(self) -> None:
        headers = [
            _header("PO1", "S1", None),
            _header("PO2", "S1", "正确名称"),
            _header("PO3", "S1", "后来者"),
        ]
        plan = buildGraphPlan(headers, [])
        names = {n["key"]: n["name"] for n in plan.suppliers}
        assert names["S1"] == "正确名称"

    def test_empty_codes_and_orphan_lines_skipped_with_stats(self) -> None:
        headers = [_header("PO1", "S1", "甲")]
        lines = [
            _line("PO1", "M1", "S1"),
            _line("", "M2", "S1"),  # 空 po_no → 跳过
            _line("PO1", "", "S1"),  # 空 material_code → 跳过
            _line("PO-ORPHAN", "M3", "S1"),  # 孤儿行：头表无此单 → 跳过
        ]

        plan = buildGraphPlan(headers, lines)

        assert {n["key"] for n in plan.materials} == {"M1"}
        assert {(e["fromKey"], e["toKey"]) for e in plan.contains} == {("PO1", "M1")}
        # 空 po_no / 空 material_code 各计 1；孤儿行单独计（不混入 skipped）
        assert plan.stats["skippedLines"] == 2
        assert plan.stats["orphanLines"] == 1

    def test_line_supplier_falls_back_to_header_map(self) -> None:
        # 行级 supplier_code 为空（NULL）→ 用头表 po_no -> supplier_code 回落
        headers = [_header("PO1", "S1", "甲")]
        lines = [_line("PO1", "M1", None)]

        plan = buildGraphPlan(headers, lines)

        assert {(e["fromKey"], e["toKey"]) for e in plan.supplies} == {("S1", "M1")}

    def test_node_rows_carry_source_and_code(self) -> None:
        plan = buildGraphPlan([_header("PO1", "S1", "甲")], [_line("PO1", "M1", "S1")])

        for node in (*plan.suppliers, *plan.purchaseOrders, *plan.materials):
            assert node["source"] == "thbi_dwd"
            assert node["code"] == node["key"]

    def test_empty_input_returns_empty_plan(self) -> None:
        plan = buildGraphPlan([], [])
        assert not plan.suppliers
        assert not plan.materials
        assert not plan.purchaseOrders
        assert not plan.places
        assert not plan.supplies
        assert not plan.contains
