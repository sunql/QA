"""字段显示名/单位解析（真实 PG）。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.domain.models import (
    DataSource,
    FeatureDefinition,
    KpiCatalog,
    OntologyClass,
    OntologyProperty,
)
from app.domain.query_plan import Aggregation, QueryPlan
from app.services import field_display_resolver
from app.services.field_display_resolver import (
    FieldDisplay,
    missingFieldHints,
    resolveFieldDisplays,
)


async def _seedDataSource(session, *, dsId: int = 9001) -> None:
    """feature_definition.datasource_id 是非空 FK，先补一行最小数据源。"""
    session.add(
        DataSource(
            id=dsId,
            name="ds-zh-labels-test",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="x",
            username="u",
            password_encrypted="x",
        )
    )
    await session.commit()


async def _seedClass(session, *, className: str = "PRECEIPT") -> OntologyClass:
    cls = OntologyClass(class_name=className, source_table="ZJTH.PRECEIPT")
    cls.properties = [
        OntologyProperty(
            property_name="SUPPLIER_CODE",
            property_alias="供应商编码",
            data_type="STRING",
            source_column="SUPPLIER_CODE",
        ),
        OntologyProperty(
            property_name="SUPPLIER_NAME",
            business_aliases=["供应商名称", "供货方"],
            data_type="STRING",
            source_column="SUPPLIER_NAME",
        ),
        OntologyProperty(
            property_name="RCV_QTY_PUU",
            property_alias="收货数量",
            unit="件",
            data_type="DECIMAL",
            source_column="RCV_QTY_PUU",
        ),
        OntologyProperty(
            property_name="PLAN_QTY",
            data_type="DECIMAL",
            source_column="PLAN_QTY",
        ),
    ]
    session.add(cls)
    await session.commit()
    await session.refresh(cls)
    return cls


def _plan(*properties: str) -> QueryPlan:
    """最小 plan：不写聚合别名（别名相关断言各自构造 plan）。"""
    return QueryPlan(
        target="测试",
        selectedClasses=["PRECEIPT"],
        selectedProperties=list(properties),
        conditions=[],
        aggregations=[],
        groupBy=[],
        joins=[],
        sortBy=[],
        rowLimit=None,
    )


class TestResolveFieldDisplays:
    async def test_ontology_alias_and_unit(self, dbSession) -> None:
        await _seedClass(dbSession)
        rows = [{"supplier_code": "B130", "rcv_qty_puu": Decimal("12")}]
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["supplier_code", "rcv_qty_puu"],
            rows=rows,
            plan=_plan("SUPPLIER_CODE", "RCV_QTY_PUU"),
        )
        assert displays["supplier_code"].label == "供应商编码"
        assert displays["supplier_code"].source == "ontology_alias"
        assert displays["supplier_code"].isNumeric is False
        assert displays["rcv_qty_puu"].label == "收货数量"
        assert displays["rcv_qty_puu"].unit == "件"
        assert displays["rcv_qty_puu"].isNumeric is True

    async def test_business_alias_fallback(self, dbSession) -> None:
        await _seedClass(dbSession)
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["supplier_name"],
            rows=[{"supplier_name": "甲"}],
            plan=_plan("SUPPLIER_NAME"),
        )
        assert displays["supplier_name"].label == "供应商名称"
        assert displays["supplier_name"].source == "business_alias"

    async def test_ontology_alias_wins_over_plan_alias(self, dbSession) -> None:
        await _seedClass(dbSession)
        plan = QueryPlan(
            target="测试",
            selectedClasses=["PRECEIPT"],
            selectedProperties=["RCV_QTY_PUU"],
            conditions=[],
            aggregations=[
                Aggregation(function="SUM", property="RCV_QTY_PUU", alias="占比")
            ],
            groupBy=[],
            joins=[],
            sortBy=[],
            rowLimit=None,
        )
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["rcv_qty_puu"],
            rows=[{"rcv_qty_puu": Decimal("1")}],
            plan=plan,
        )
        assert displays["rcv_qty_puu"].label == "收货数量"
        assert displays["rcv_qty_puu"].source == "ontology_alias"

    async def test_plan_alias_used_when_ontology_missing(self, dbSession) -> None:
        await _seedClass(dbSession)
        plan = QueryPlan(
            target="测试",
            selectedClasses=["PRECEIPT"],
            selectedProperties=["PLAN_QTY"],
            conditions=[],
            aggregations=[
                Aggregation(function="SUM", property="PLAN_QTY", alias="计划数量")
            ],
            groupBy=[],
            joins=[],
            sortBy=[],
            rowLimit=None,
        )
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["plan_qty"],
            rows=[{"plan_qty": Decimal("1")}],
            plan=plan,
        )
        assert displays["plan_qty"].label == "计划数量"
        assert displays["plan_qty"].source == "plan_alias"

    async def test_unresolved_column_is_none(self, dbSession) -> None:
        await _seedClass(dbSession)
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["total_rcv_qty"],
            rows=[{"total_rcv_qty": Decimal("5")}],
            plan=_plan("RCV_QTY_PUU"),
        )
        # 聚合结果列名与属性名不相等 ⇒ 不猜，落空并等待提示
        assert displays["total_rcv_qty"].label is None
        assert displays["total_rcv_qty"].source == "none"

    async def test_numeric_ignores_bool(self, dbSession) -> None:
        await _seedClass(dbSession)
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["is_ok"],
            rows=[{"is_ok": True}],
            plan=_plan("RCV_QTY_PUU"),
        )
        assert displays["is_ok"].isNumeric is False

    async def test_no_context_returns_empty(self, dbSession) -> None:
        assert (
            await resolveFieldDisplays(
                dbSession, columns=["x"], rows=[{"x": 1}], plan=None, sourceTable=None
            )
            == {}
        )

    async def test_ambiguous_property_label_is_none(self, dbSession) -> None:
        await _seedClass(dbSession)
        other = OntologyClass(class_name="PRECEIPT_2", source_table="ZJTH.PRECEIPT_2")
        other.properties = [
            OntologyProperty(
                property_name="SUPPLIER_CODE",
                property_alias="供货商编号",
                data_type="STRING",
                source_column="SUPPLIER_CODE",
            )
        ]
        dbSession.add(other)
        await dbSession.commit()
        plan = QueryPlan(
            target="测试",
            selectedClasses=["PRECEIPT", "PRECEIPT_2"],
            selectedProperties=["SUPPLIER_CODE"],
            conditions=[],
            aggregations=[],
            groupBy=[],
            joins=[],
            sortBy=[],
            rowLimit=None,
        )
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["supplier_code"],
            rows=[{"supplier_code": "B130"}],
            plan=plan,
        )
        assert displays["supplier_code"].label is None
        assert displays["supplier_code"].source == "none"

    async def test_kpi_unit_by_exact_code(self, dbSession) -> None:
        await _seedClass(dbSession)
        # PLAN_QTY 在本体里没有 unit ⇒ 单位只能来自 KPI 目录
        dbSession.add(
            KpiCatalog(
                kpi_code="PLAN_QTY",
                kpi_name="计划数量",
                unit="吨",
                owner="test",
            )
        )
        await dbSession.commit()
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["plan_qty"],
            rows=[{"plan_qty": Decimal("1")}],
            plan=_plan("PLAN_QTY"),
        )
        assert displays["plan_qty"].unit == "吨"

    async def test_feature_unit_by_exact_name(self, dbSession) -> None:
        await _seedClass(dbSession)
        await _seedDataSource(dbSession)
        # 该列在本体里既无属性也无单位 ⇒ 单位只能来自特征定义
        dbSession.add(
            FeatureDefinition(
                feature_name="rcv_qty_feature",
                feature_alias="收货数量特征",
                entity_type="SUPPLIER",
                calculation_logic="sum(rcv_qty)",
                unit="件",
                datasource_id=9001,
            )
        )
        await dbSession.commit()
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["rcv_qty_feature"],
            rows=[{"rcv_qty_feature": Decimal("1")}],
            plan=None,
            sourceTable="ZJTH.PRECEIPT",
        )
        assert displays["rcv_qty_feature"].unit == "件"
        assert displays["rcv_qty_feature"].label is None

    async def test_ambiguous_unit_is_dropped(self, dbSession) -> None:
        await _seedClass(dbSession)
        dbSession.add_all(
            [
                KpiCatalog(kpi_code="PLAN_QTY", kpi_name="计划数量", unit="吨", owner="a"),
                KpiCatalog(kpi_code="PLAN_QTY_2", kpi_name="PLAN_QTY", unit="箱", owner="b"),
            ]
        )
        await dbSession.commit()
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["plan_qty"],
            rows=[{"plan_qty": Decimal("1")}],
            plan=_plan("PLAN_QTY"),
        )
        # 同一名字命中两个不同单位 ⇒ 不猜
        assert displays["plan_qty"].unit is None

    async def test_db_failure_leaves_caller_session_usable(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """降级点必须只回滚自己的语句，不污染调用方事务（Task 5 复用请求会话）。"""
        await _seedClass(dbSession)

        async def _boom(session, class_ids):
            await session.execute(text("SELECT * FROM table_that_does_not_exist"))
            return []

        monkeypatch.setattr(field_display_resolver, "_loadProperties", _boom)

        displays = await resolveFieldDisplays(
            dbSession,
            columns=["supplier_code"],
            rows=[{"supplier_code": "B130"}],
            plan=_plan("SUPPLIER_CODE"),
        )
        assert displays == {}
        assert (await dbSession.execute(text("SELECT 1"))).scalar_one() == 1


class TestMissingFieldHints:
    def test_lists_only_unresolved_and_unitless_numeric(self) -> None:
        displays = {
            "a": FieldDisplay(label="甲", unit=None, isNumeric=True, source="ontology_alias"),
            "b": FieldDisplay(label=None, unit=None, isNumeric=False, source="none"),
            "c": FieldDisplay(label=None, unit=None, isNumeric=True, source="none"),
        }
        hints = missingFieldHints({"a": "甲"}, displays)
        assert hints["missingLabels"] == ["b", "c"]
        assert hints["missingUnits"] == ["a", "c"]
