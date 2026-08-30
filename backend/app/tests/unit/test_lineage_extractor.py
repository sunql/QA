"""lineage_extractor 单元测试（Phase 2.2 RED）。

从 ontology JOIN + Metric formula 推断 DataLineage 边。

覆盖：
- JOIN → 字段级血缘（source/target_field 都填）
- JOIN → 表级血缘（无 source/target_field 补 NULL）
- Metric formula → 指标层血缘
- 列解析失败（formula 异常）：跳过该 metric，不阻断其他
- 重复边去重（同一对上下游已存在 → 不重复生成）
- 现有 data_lineage 已存在边 → 不重复
- 层映射：当前 ontology 数据全部为 SOURCE_SYSTEM 层（Phase 3+ ODS 层落地后再扩展）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import LineageLayer, RefreshFrequency
from app.domain.models import DataLineage, OntologyClass, OntologyJoin, OntologyMetric
from app.services.lineage_extractor import (
    ExtractedEdge,
    extractEdges,
    layerForSourceTable,
)


# ---- 辅助 fake session ----


class _FakeSession:
    """最小 fake session：模拟 select().where() 与 session.add/commit。"""

    def __init__(
        self,
        classes: list[OntologyClass] | None = None,
        joins: list[OntologyJoin] | None = None,
        metrics: list[OntologyMetric] | None = None,
        existing: list[DataLineage] | None = None,
    ) -> None:
        self.classes = classes or []
        self.joins = joins or []
        self.metrics = metrics or []
        self.existing = existing or []
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, stmt):
        # 简化：根据 stmt 描述的 table 选择返回对应数据
        description = str(stmt).lower()
        if "ontology_class" in description:
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: list(self.classes))
            )
        if "ontology_join" in description:
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: list(self.joins))
            )
        if "ontology_metric" in description:
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: list(self.metrics))
            )
        if "data_lineage" in description:
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: list(self.existing))
            )
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [])
        )

    def add(self, entity):
        self.added.append(entity)

    async def commit(self):
        self.commits += 1


def _makeClass(
    id: int, name: str, source_table: str | None = None
) -> OntologyClass:
    return OntologyClass(
        id=id, class_name=name, source_table=source_table or name, version=1
    )


def _makeJoin(
    id: int,
    src_class: OntologyClass,
    tgt_class: OntologyClass,
    src_cols: list[str],
    tgt_cols: list[str],
    relation_type: str = "business",
    description: str | None = None,
) -> OntologyJoin:
    return OntologyJoin(
        id=id,
        source_class_id=src_class.id,
        target_class_id=tgt_class.id,
        source_columns=src_cols,
        target_columns=tgt_cols,
        join_type="INNER",
        relation_type=relation_type,
        join_key="|".join(f"{s}={t}" for s, t in zip(src_cols, tgt_cols, strict=False)),
        description=description,
    )


def _makeMetric(
    id: int,
    name: str,
    formula: str,
    target_class: OntologyClass | None,
    agg_function: str = "SUM",
) -> OntologyMetric:
    return OntologyMetric(
        id=id,
        metric_name=name,
        formula=formula,
        agg_function=agg_function,
        target_class_id=target_class.id if target_class else None,
    )


# ---- layerForSourceTable 单独测试 ----


class TestLayerForSourceTable:
    def test_known_source_table_returns_source_system(self):
        assert layerForSourceTable("PORDER") == LineageLayer.SOURCE_SYSTEM

    def test_uppercase_normalized(self):
        assert layerForSourceTable("porder") == LineageLayer.SOURCE_SYSTEM

    def test_unknown_table_defaults_to_source_system(self):
        """未识别表名默认 SOURCE_SYSTEM（不阻断，保持 best-effort）。"""
        assert layerForSourceTable("UNKNOWN_TBL") == LineageLayer.SOURCE_SYSTEM

    def test_empty_returns_source_system(self):
        assert layerForSourceTable("") == LineageLayer.SOURCE_SYSTEM


# ---- extractEdges：JOIN → DataLineage ----


class TestExtractFromJoins:
    async def test_field_level_edge_from_join(self):
        cls_porder = _makeClass(1, "PURCHASE_ORDER", "PORDER")
        cls_supplier = _makeClass(2, "SUPPLIER", "BPSUPPLIER")
        join = _makeJoin(
            id=10,
            src_class=cls_porder,
            tgt_class=cls_supplier,
            src_cols=["BPSNUM"],
            tgt_cols=["BPSNUM"],
            description="订单 → 供应商",
        )
        session = _FakeSession(classes=[cls_porder, cls_supplier], joins=[join])

        edges = await extractEdges(session)

        assert len(edges) == 1
        edge = edges[0]
        assert isinstance(edge, ExtractedEdge)
        assert edge.source_layer == LineageLayer.SOURCE_SYSTEM
        assert edge.source_system == "ERP"
        assert edge.source_object == "PORDER"
        assert edge.source_field == "BPSNUM"
        assert edge.target_layer == LineageLayer.SOURCE_SYSTEM
        assert edge.target_object == "BPSUPPLIER"
        assert edge.target_field == "BPSNUM"
        assert edge.transformation_rule is not None
        assert "JOIN" in edge.transformation_rule

    async def test_multi_column_join_emits_multiple_edges(self):
        """多列 JOIN：每对 (src_cols[i], tgt_cols[i]) 产生一条边。"""
        cls_a = _makeClass(1, "A", "TA")
        cls_b = _makeClass(2, "B", "TB")
        join = _makeJoin(
            id=10,
            src_class=cls_a,
            tgt_class=cls_b,
            src_cols=["K1", "K2"],
            tgt_cols=["K1", "K2"],
        )
        session = _FakeSession(classes=[cls_a, cls_b], joins=[join])

        edges = await extractEdges(session)

        # 2 列 → 2 条边
        assert len(edges) == 2
        fields = sorted([(e.source_field, e.target_field) for e in edges])
        assert fields == [("K1", "K1"), ("K2", "K2")]

    async def test_skips_join_with_missing_class(self):
        """JOIN 引用了不存在的 class（数据不一致）：跳过，不抛错。"""
        cls_porder = _makeClass(1, "PURCHASE_ORDER", "PORDER")
        join = OntologyJoin(
            id=10,
            source_class_id=1,
            target_class_id=999,  # 不存在
            source_columns=["BPSNUM"],
            target_columns=["BPSNUM"],
            join_type="INNER",
            relation_type="business",
            join_key="BPSNUM=BPSNUM",
        )
        session = _FakeSession(classes=[cls_porder], joins=[join])

        edges = await extractEdges(session)

        assert edges == []


# ---- extractEdges：Metric formula → DataLineage ----


class TestExtractFromMetrics:
    async def test_metric_extracts_source_columns(self):
        cls_porder = _makeClass(1, "PURCHASE_ORDER", "PORDER")
        metric = _makeMetric(
            id=20,
            name="TOTAL_ORDER_QTY",
            formula="SUM(t.ORDER_QTY)",
            target_class=cls_porder,
        )
        session = _FakeSession(classes=[cls_porder], metrics=[metric])

        edges = await extractEdges(session)

        assert len(edges) == 1
        edge = edges[0]
        # 源端来自 formula 解析：表别名 t → 对应 PORDER 的 source_table
        # 目标端：metric 的 target_class → KPI 层（因为是聚合指标）
        assert edge.target_layer == LineageLayer.KPI
        assert edge.source_field == "ORDER_QTY"
        assert edge.transformation_rule == "SUM(t.ORDER_QTY)"

    async def test_metric_with_multiple_columns(self):
        cls_porder = _makeClass(1, "PURCHASE_ORDER", "PORDER")
        metric = _makeMetric(
            id=20,
            name="OTD_RATE",
            formula="SUM(CASE WHEN t.ACTUAL_DATE <= t.PROMISE_DATE THEN 1 ELSE 0 END) / COUNT(t.ID)",
            target_class=cls_porder,
        )
        session = _FakeSession(classes=[cls_porder], metrics=[metric])

        edges = await extractEdges(session)

        # 4 个不同的列引用
        fields = sorted({e.source_field for e in edges})
        assert fields == ["ACTUAL_DATE", "ID", "PROMISE_DATE"]

    async def test_metric_with_unparsable_formula_skips_silently(self):
        """formula 解析失败：跳过该 metric，不阻断其他 metric。"""
        cls_a = _makeClass(1, "A", "TA")
        good = _makeMetric(id=20, name="GOOD", formula="SUM(t.QTY)", target_class=cls_a)
        bad = _makeMetric(id=21, name="BAD", formula="((((" , target_class=cls_a)  # syntax error
        session = _FakeSession(classes=[cls_a], metrics=[good, bad])

        edges = await extractEdges(session)

        # good 仍能解析
        assert any(e.transformation_rule == "SUM(t.QTY)" for e in edges)


# ---- 去重 ----


class TestDeduplication:
    async def test_does_not_re_extract_existing_edge(self):
        cls_a = _makeClass(1, "A", "TA")
        cls_b = _makeClass(2, "B", "TB")
        join = _makeJoin(
            id=10, src_class=cls_a, tgt_class=cls_b,
            src_cols=["K"], tgt_cols=["K"],
        )
        existing_edge = DataLineage(
            id=100,
            source_layer=LineageLayer.SOURCE_SYSTEM,
            source_system="ERP",
            source_object="TA",
            source_field="K",
            target_layer=LineageLayer.SOURCE_SYSTEM,
            target_system="ERP",
            target_object="TB",
            target_field="K",
        )
        session = _FakeSession(
            classes=[cls_a, cls_b],
            joins=[join],
            existing=[existing_edge],
        )

        edges = await extractEdges(session)

        # 已有 → 不返回
        assert edges == []

    async def test_dedup_within_same_run(self):
        """同一 ONTOLOGY 中同上下游边只生成一次。"""
        cls_a = _makeClass(1, "A", "TA")
        cls_b = _makeClass(2, "B", "TB")
        j1 = _makeJoin(
            id=10, src_class=cls_a, tgt_class=cls_b,
            src_cols=["K"], tgt_cols=["K"],
        )
        j2 = _makeJoin(
            id=11, src_class=cls_a, tgt_class=cls_b,
            src_cols=["K"], tgt_cols=["K"],  # 重复
        )
        session = _FakeSession(classes=[cls_a, cls_b], joins=[j1, j2])

        edges = await extractEdges(session)

        # 同上下游 + 字段组合去重
        assert len(edges) == 1


# ---- 完整性 ----


class TestExtractEdgesContract:
    async def test_empty_ontology_returns_empty(self):
        session = _FakeSession()
        edges = await extractEdges(session)
        assert edges == []

    async def test_extracted_edge_carries_refresh_frequency(self):
        """所有抽取出的边默认 refresh_frequency=DAILY（与 data_lineage 默认一致）。"""
        cls_a = _makeClass(1, "A", "TA")
        cls_b = _makeClass(2, "B", "TB")
        join = _makeJoin(
            id=10, src_class=cls_a, tgt_class=cls_b,
            src_cols=["K"], tgt_cols=["K"],
        )
        session = _FakeSession(classes=[cls_a, cls_b], joins=[join])

        edges = await extractEdges(session)

        assert edges[0].refresh_frequency == RefreshFrequency.DAILY

    async def test_extracted_edge_has_transformation_rule(self):
        """transformation_rule 不为空：便于运维溯源。"""
        cls_a = _makeClass(1, "A", "TA")
        cls_b = _makeClass(2, "B", "TB")
        join = _makeJoin(
            id=10, src_class=cls_a, tgt_class=cls_b,
            src_cols=["K"], tgt_cols=["K"],
            description="业务关联",
        )
        session = _FakeSession(classes=[cls_a, cls_b], joins=[join])

        edges = await extractEdges(session)

        assert edges[0].transformation_rule is not None
        assert len(edges[0].transformation_rule) > 0