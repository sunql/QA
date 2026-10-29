"""Task 12 纯函数单测：`splitLegacyTableNames` / `buildTableIndex`。

这些函数是「裸表名 → 数据源」精确 join 的全部逻辑边界（分隔符、大小写、
脏 JSONB 条目、同一张表被多个数据源持有），放在 unit 层以免每次都跑 PG。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.models import SchemaCache
from app.services.kpi_datasource_suggestion_service import (
    KpiDatasourceSuggestionService,
    SuggestionContext,
    buildDatasourceNamePattern,
    buildTableIndex,
    splitLegacyTableNames,
    stripDatasourceNames,
)


def _schemaRow(datasourceId: int, tables: list[object]) -> SchemaCache:
    return SchemaCache(
        id=datasourceId,
        datasource_id=datasourceId,
        schema_name="",
        schema_data=tables,  # type: ignore[arg-type]
        schema_version="md5-unit",
    )


class TestSplitLegacyTableNames:
    def test_returns_empty_for_none(self) -> None:
        assert splitLegacyTableNames(None) == ()

    def test_returns_empty_for_blank_text(self) -> None:
        assert splitLegacyTableNames("   ") == ()

    def test_single_table_name_is_upper_cased(self) -> None:
        assert splitLegacyTableNames("dwd_payment") == ("DWD_PAYMENT",)

    def test_splits_on_space_and_plus(self) -> None:
        assert splitLegacyTableNames("DWD_PURCHASE_ORDER + DWD_GOODS_RECEIPT") == (
            "DWD_PURCHASE_ORDER",
            "DWD_GOODS_RECEIPT",
        )

    def test_splits_on_comma_and_ideographic_comma(self) -> None:
        assert splitLegacyTableNames("T_A, T_B、T_C") == ("T_A", "T_B", "T_C")

    def test_splits_on_plus_without_spaces(self) -> None:
        """存量真实形态可能是 ``"A+B"``（无空格）⇒ '+' 必须独立成分隔符。

        之前所有用例都写成 ``"A + B"``（带空格），空格就能拆开 ⇒ '+'
        被「空格」掩盖，从没被真正测到。
        """
        assert splitLegacyTableNames("T_A+T_B") == ("T_A", "T_B")

    def test_deduplicates_while_preserving_order(self) -> None:
        assert splitLegacyTableNames("T_B T_A T_B") == ("T_B", "T_A")

    def test_strips_surrounding_whitespace(self) -> None:
        assert splitLegacyTableNames("  T_A  ") == ("T_A",)


class TestBuildDatasourceNamePattern:
    """只对**含空白**的数据源名建模式（B-3 的核心约束）。"""

    def test_pattern_is_none_when_no_spaced_name(self) -> None:
        assert buildDatasourceNamePattern(["DWD_PAYMENT", "TABLE_JOIN_DS"]) is None

    def test_pattern_skips_single_word_names(self) -> None:
        # 单字名同时是合法裸表名 ⇒ 摘掉会丢 legacy_table_exact（既有测试依赖）
        assert buildDatasourceNamePattern(["DWD_PAYMENT"]) is None

    def test_pattern_matches_spaced_name_with_any_inner_whitespace(self) -> None:
        pattern = buildDatasourceNamePattern(["THBI Oracle"])

        assert pattern is not None
        assert splitLegacyTableNames("THBI  Oracle ORDERS", pattern) == ("ORDERS",)

    def test_pattern_respects_word_boundaries(self) -> None:
        pattern = buildDatasourceNamePattern(["THBI Oracle"])

        assert pattern is not None
        # 含下划线的标识不能被当成独立词命中
        assert stripDatasourceNames("THBI_ORDERS", pattern) == "THBI_ORDERS"
        assert stripDatasourceNames("XTHBI ORACLE", pattern) == "XTHBI ORACLE"
        assert stripDatasourceNames("THBI ORACLEX", pattern) == "THBI ORACLEX"

    def test_pattern_prefers_longer_name_first(self) -> None:
        pattern = buildDatasourceNamePattern(["A B", "A B C"])

        assert pattern is not None
        # 长名整体摘掉，而不是被短名先吃掉前缀留下 " C"
        assert stripDatasourceNames("A B C", pattern).strip() == ""


class TestStripDatasourceNames:
    """拆表名前先摘掉含空白的数据源名跨度（B-3）。"""

    def test_split_removes_spaced_datasource_name_span(self) -> None:
        pattern = buildDatasourceNamePattern(["THBI Oracle"])

        assert splitLegacyTableNames("THBI Oracle ORDERS", pattern) == ("ORDERS",)

    def test_split_keeps_tokens_when_pattern_is_none(self) -> None:
        # 不传 pattern ⇒ 旧行为逐位不变（回归护栏）
        assert splitLegacyTableNames("THBI Oracle ORDERS") == (
            "THBI",
            "ORACLE",
            "ORDERS",
        )

    def test_split_keeps_single_word_datasource_name_as_table(self) -> None:
        pattern = buildDatasourceNamePattern(["DWD_PAYMENT"])

        assert pattern is None
        assert splitLegacyTableNames("DWD_PAYMENT", pattern) == ("DWD_PAYMENT",)

    def test_split_preserves_space_separated_tables(self) -> None:
        pattern = buildDatasourceNamePattern(["THBI Oracle"])

        assert splitLegacyTableNames("T_A T_B", pattern) == ("T_A", "T_B")

    def test_strip_replaces_with_space_not_empty(self) -> None:
        pattern = buildDatasourceNamePattern(["THBI Oracle"])

        # 替换成空串会把两侧粘成 "AB"；替换成空格才能保持分隔
        assert splitLegacyTableNames("A THBI Oracle B", pattern) == ("A", "B")


class TestBuildTableIndex:
    def test_empty_rows_produce_empty_index(self) -> None:
        assert buildTableIndex(()) == {}

    def test_indexes_table_name_upper_cased(self) -> None:
        rows = (_schemaRow(1, [{"table_name": "dwd_payment"}]),)

        assert buildTableIndex(rows) == {"DWD_PAYMENT": (1,)}

    def test_skips_non_dict_entries(self) -> None:
        rows = (_schemaRow(1, ["BAD", None, 123, {"table_name": "T_A"}]),)

        assert buildTableIndex(rows) == {"T_A": (1,)}

    def test_skips_entries_without_table_name(self) -> None:
        rows = (_schemaRow(1, [{}, {"table_name": "  "}, {"table_name": "T_A"}]),)

        assert buildTableIndex(rows) == {"T_A": (1,)}

    def test_skips_null_table_name_instead_of_indexing_none(self) -> None:
        """``table_name: null`` 不能被 ``str(None)`` 转成 ``"NONE"`` 进索引。

        否则 KPI 的 ``data_source`` 恰好是 ``"none"`` 时会冒出一条 0.85 的假建议。
        """
        rows = (_schemaRow(1, [{"table_name": None, "columns": []}]),)

        index = buildTableIndex(rows)

        assert "NONE" not in index
        assert index == {}

    def test_accepts_camel_case_table_name_key(self) -> None:
        """历史行用驼峰 ``tableName`` 时也必须能建索引（与 datasource.py 口径一致）。"""
        rows = (_schemaRow(1, [{"tableName": "T_A", "columns": []}]),)

        assert buildTableIndex(rows) == {"T_A": (1,)}

    def test_skips_non_string_table_name(self) -> None:
        """数字/布尔等脏值直接跳过（不被 ``str()`` 转成表名）。"""
        rows = (_schemaRow(1, [{"table_name": 123}, {"table_name": True}]),)

        assert buildTableIndex(rows) == {}

    def test_same_table_in_multiple_sources_yields_sorted_ids(self) -> None:
        rows = (_schemaRow(2, [{"table_name": "T_A"}]), _schemaRow(1, [{"table_name": "t_a"}]))

        assert buildTableIndex(rows) == {"T_A": (1, 2)}

    def test_same_source_multiple_tables_are_aggregated(self) -> None:
        rows = (_schemaRow(1, [{"table_name": "T_A"}, {"table_name": "T_B"}]),)

        assert buildTableIndex(rows) == {"T_A": (1,), "T_B": (1,)}

    def test_rows_without_datasource_id_are_skipped(self) -> None:
        rows = (_schemaRow(1, [{"table_name": "T_A"}]),)
        rows[0].datasource_id = None  # type: ignore[assignment]

        assert buildTableIndex(rows) == {}


class _StubSession:
    """只实现 ``get`` 的假 session（``_ontologyDatasourceIds`` 唯一的 DB 依赖）。"""

    def __init__(self, entities: dict[tuple[str, int], Any]) -> None:
        self.entities = entities

    async def get(self, model: Any, ident: int) -> Any:
        return self.entities.get((model.__name__, ident))


class _FakeMetric:
    def __init__(self, targetClassId: int | None) -> None:
        self.target_class_id = targetClassId


class _FakeClass:
    def __init__(self, sourceTable: str | None, validTo: Any = None) -> None:
        self.source_table = sourceTable
        self.valid_to = validTo


class TestOntologyDatasourceIds:
    @staticmethod
    def _context() -> SuggestionContext:
        return SuggestionContext(
            sources=(),
            schemaRows=(),
            tableDatasourceIds={"T_A": (7,)},
        )

    @pytest.mark.asyncio
    async def test_no_metric_id_returns_empty(self) -> None:
        session = _StubSession({})

        assert (
            await KpiDatasourceSuggestionService._ontologyDatasourceIds(
                session, None, self._context()
            )
            == set()
        )

    @pytest.mark.asyncio
    async def test_missing_metric_returns_empty(self) -> None:
        session = _StubSession({})

        assert (
            await KpiDatasourceSuggestionService._ontologyDatasourceIds(
                session, 42, self._context()
            )
            == set()
        )

    @pytest.mark.asyncio
    async def test_metric_without_target_class_returns_empty(self) -> None:
        session = _StubSession({("OntologyMetric", 1): _FakeMetric(None)})

        assert (
            await KpiDatasourceSuggestionService._ontologyDatasourceIds(
                session, 1, self._context()
            )
            == set()
        )

    @pytest.mark.asyncio
    async def test_class_without_source_table_returns_empty(self) -> None:
        session = _StubSession(
            {
                ("OntologyMetric", 1): _FakeMetric(2),
                ("OntologyClass", 2): _FakeClass("  "),
            }
        )

        assert (
            await KpiDatasourceSuggestionService._ontologyDatasourceIds(
                session, 1, self._context()
            )
            == set()
        )

    @pytest.mark.asyncio
    async def test_tombstoned_class_returns_empty(self) -> None:
        session = _StubSession(
            {
                ("OntologyMetric", 1): _FakeMetric(2),
                ("OntologyClass", 2): _FakeClass("T_A", validTo="2026-01-01"),
            }
        )

        assert (
            await KpiDatasourceSuggestionService._ontologyDatasourceIds(
                session, 1, self._context()
            )
            == set()
        )

    @pytest.mark.asyncio
    async def test_source_table_resolves_to_datasource_ids(self) -> None:
        session = _StubSession(
            {
                ("OntologyMetric", 1): _FakeMetric(2),
                ("OntologyClass", 2): _FakeClass(" t_a "),
            }
        )

        assert await KpiDatasourceSuggestionService._ontologyDatasourceIds(
            session, 1, self._context()
        ) == {7}
