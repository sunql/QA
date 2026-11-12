"""P3 时间粒度 —— TimeGrain 枚举 + kpi_catalog/ontology_metric 列形状（纯元数据，不碰库）。

D1（0143）：治理侧显式声明「这个 KPI 按月/季度/年…口径」，并要求解析到源类的日期列。
两侧（ORM / 迁移）必须逐字一致，否则 drift / autogenerate 会提议 DROP 或 alter。
"""

from __future__ import annotations

from app.domain.enums import TIME_GRAIN_VALUES, TimeGrain
from app.domain.models import KpiCatalog, OntologyMetric

_TIME_GRAIN_COMMENT = "0143，P3 时间粒度"


def test_time_grain_enum_has_five_values() -> None:
    assert [e.value for e in TimeGrain] == [
        "day",
        "week",
        "month",
        "quarter",
        "year",
    ]


def test_time_grain_values_frozenset() -> None:
    assert isinstance(TIME_GRAIN_VALUES, frozenset)
    assert frozenset({"day", "week", "month", "quarter", "year"}) == TIME_GRAIN_VALUES
    # 枚举 ↔ 值集合一致（enums.py 里集合是字面量，防两处漂移）。
    assert frozenset(member.value for member in TimeGrain) == TIME_GRAIN_VALUES


def test_kpi_catalog_declares_time_grain() -> None:
    column = KpiCatalog.__table__.columns["time_grain"]
    assert column.nullable is True
    assert column.type.length == 20
    assert column.comment == _TIME_GRAIN_COMMENT


def test_ontology_metric_declares_time_grain() -> None:
    column = OntologyMetric.__table__.columns["time_grain"]
    assert column.nullable is True
    assert column.type.length == 20
    assert column.comment == _TIME_GRAIN_COMMENT


def test_ontology_metric_declares_time_column() -> None:
    column = OntologyMetric.__table__.columns["time_column"]
    assert column.nullable is True
    assert column.type.length == 100
    assert column.comment == _TIME_GRAIN_COMMENT
