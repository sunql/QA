"""值域采样（2-1）单元测试。

覆盖 ValueSampler：候选列筛选（类型/标识符白名单/数量封顶）、各方言去重查询语法、
高基数探测、单列/整体降级、有界缓存、以及本体数据注入采样 SQL 的防御。
"""

from __future__ import annotations

import re

import pytest

from app.domain.enums import DataSourceType
from app.domain.models import OntologyClass, OntologyProperty
from app.services.nl2sql_service import Nl2SqlService
from app.services.value_sampler import (
    ValueSampler,
    _VALUE_SAMPLE_DISTINCT_LIMIT,
    _VALUE_SAMPLE_MAX_COLUMNS,
    clearValueSampleCache,
)


@pytest.fixture(autouse=True)
def _isolate_value_sample_cache():
    """模块级采样缓存按测试隔离，避免跨测试缓存命中。"""
    clearValueSampleCache()
    yield
    clearValueSampleCache()


def _prop(name: str, dataType: str, column: str) -> OntologyProperty:
    return OntologyProperty(property_name=name, data_type=dataType, source_column=column)


def _cls(name: str, table: str, *props: OntologyProperty) -> OntologyClass:
    return OntologyClass(class_name=name, source_table=table, properties=list(props))


class _FakeDb:
    """按列返回去重值；记录每次采样 SQL；可指定列抛错。"""

    def __init__(self, values: dict[str, list[str]], *, failCols: set[str] | None = None):
        self.values = values
        self.failCols = failCols or set()
        self.calls: list[str] = []

    async def execute(self, sql: str) -> list[dict]:
        self.calls.append(sql)
        match = re.search(r"DISTINCT\s+(\w+)", sql)
        col = match.group(1) if match else ""
        if col in self.failCols:
            raise RuntimeError("业务库不可达")
        return [{col: v} for v in self.values.get(col, [])]


def _dialect(type_: DataSourceType, oracle_version: str | None = None):
    return Nl2SqlService.resolveDialect(type_, oracle_version)


class TestBoundedDistinctDialects:
    def test_mysql_uses_limit(self) -> None:
        sql = _dialect(DataSourceType.MYSQL).boundedDistinct("sales", "status", 11)
        assert sql == "SELECT DISTINCT status FROM sales LIMIT 11"

    def test_postgresql_uses_limit(self) -> None:
        sql = _dialect(DataSourceType.POSTGRESQL).boundedDistinct("sales", "status", 11)
        assert sql == "SELECT DISTINCT status FROM sales LIMIT 11"

    def test_oracle_11g_uses_rownum_wrapper(self) -> None:
        sql = _dialect(DataSourceType.ORACLE, "Oracle 11g").boundedDistinct("T", "status", 11)
        assert sql == "SELECT * FROM (SELECT DISTINCT status FROM T) WHERE ROWNUM <= 11"

    def test_oracle_12c_uses_fetch_first(self) -> None:
        sql = _dialect(DataSourceType.ORACLE, "Oracle 19c").boundedDistinct("T", "status", 11)
        assert sql == "SELECT DISTINCT status FROM T FETCH FIRST 11 ROWS ONLY"


class TestCandidateSelection:
    def test_only_string_datetime_columns_are_candidates(self) -> None:
        cls = _cls("X", "T",
                   _prop("name", "STRING", "NAME"),
                   _prop("qty", "DECIMAL", "QTY"),
                   _prop("ts", "DATETIME", "TS"),
                   _prop("flag", "INT", "FLAG"))
        sampler = ValueSampler(_FakeDb({}).execute, dialect=_dialect(DataSourceType.MYSQL))
        cands = sampler._candidates([cls])
        cols = {p.source_column for _, p in cands}
        assert cols == {"NAME", "TS"}

    def test_rejects_non_identifier_tables_and_columns(self) -> None:
        # 注入尝试：表/列含分号或引号，应被白名单拒绝，不进候选
        cls = _cls("X", "T; DROP TABLE t",
                   _prop("ok", "STRING", "OK"),
                   _prop("bad", "STRING", "a'; DROP TABLE t;--"))
        sampler = ValueSampler(_FakeDb({}).execute, dialect=_dialect(DataSourceType.MYSQL))
        cands = sampler._candidates([cls])
        assert [(c.source_table, p.source_column) for c, p in cands] == []

    def test_caps_candidate_count(self) -> None:
        props = [_prop(f"p{i}", "STRING", f"P{i}") for i in range(10)]
        cls = _cls("X", "T", *props)
        sampler = ValueSampler(_FakeDb({}).execute, dialect=_dialect(DataSourceType.MYSQL))
        assert len(sampler._candidates([cls])) == _VALUE_SAMPLE_MAX_COLUMNS

    def test_skips_unmapped_column(self) -> None:
        cls = _cls("X", "T", OntologyProperty(property_name="p", data_type="STRING"))
        sampler = ValueSampler(_FakeDb({}).execute, dialect=_dialect(DataSourceType.MYSQL))
        assert sampler._candidates([cls]) == []


class TestSampling:
    async def test_returns_distinct_values_per_column(self) -> None:
        db = _FakeDb({"STATUS": ["1", "A", "2"], "REGION": ["华东", "华北"]})
        sampler = ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.MYSQL))
        cls = _cls("X", "T", _prop("status", "STRING", "STATUS"), _prop("region", "STRING", "REGION"))
        result = await sampler.sample([cls])
        assert result == {("T", "STATUS"): ["1", "A", "2"], ("T", "REGION"): ["华东", "华北"]}
        assert len(db.calls) == 2

    async def test_bakes_schema_prefix_for_oracle(self) -> None:
        db = _FakeDb({"STATUS": ["1"]})
        sampler = ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.ORACLE, "12c"), safePrefix="ZJTH")
        cls = _cls("X", "PRECEIPT", _prop("status", "STRING", "STATUS"))
        result = await sampler.sample([cls])
        assert result == {("PRECEIPT", "STATUS"): ["1"]}
        assert "SELECT DISTINCT STATUS FROM ZJTH.PRECEIPT" in db.calls[0]

    async def test_does_not_double_prefix_matching_qualifier(self) -> None:
        """2-1：表名已带同一 schema 前缀时不重复烘焙。"""
        db = _FakeDb({"STATUS": ["1"]})
        sampler = ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.ORACLE, "12c"), safePrefix="ZJTH")
        cls = _cls("X", "ZJTH.PRECEIPT", _prop("status", "STRING", "STATUS"))
        await sampler.sample([cls])
        assert "FROM ZJTH.PRECEIPT" in db.calls[0]
        assert "ZJTH.ZJTH" not in db.calls[0]

    async def test_different_qualifier_still_bakes_prefix(self) -> None:
        """2-1：表名带的是其他 schema 限定时仍烘焙数据源前缀，与 schema 文本路径一致。"""
        db = _FakeDb({"STATUS": ["1"]})
        sampler = ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.ORACLE, "12c"), safePrefix="ZJTH")
        cls = _cls("X", "OTHER.PRECEIPT", _prop("status", "STRING", "STATUS"))
        await sampler.sample([cls])
        assert "FROM ZJTH.OTHER.PRECEIPT" in db.calls[0]

    async def test_skips_high_cardinality_column(self) -> None:
        many = [str(i) for i in range(_VALUE_SAMPLE_DISTINCT_LIMIT + 1)]
        db = _FakeDb({"STATUS": many, "REGION": ["华东"]})
        sampler = ValueSampler(db.execute, dialect=_dialect(DataSourceType.MYSQL))
        cls = _cls("X", "T", _prop("status", "STRING", "STATUS"), _prop("region", "STRING", "REGION"))
        result = await sampler.sample([cls])
        assert "STATUS" not in {col for _, col in result}
        assert result == {("T", "REGION"): ["华东"]}

    async def test_degrades_per_column_on_db_error(self) -> None:
        db = _FakeDb({"STATUS": ["1"], "REGION": ["华东"]}, failCols={"REGION"})
        sampler = ValueSampler(db.execute, dialect=_dialect(DataSourceType.MYSQL))
        cls = _cls("X", "T", _prop("status", "STRING", "STATUS"), _prop("region", "STRING", "REGION"))
        result = await sampler.sample([cls])
        assert result == {("T", "STATUS"): ["1"]}  # 失败列被跳过，不抛出

    async def test_empty_classes_returns_empty(self) -> None:
        sampler = ValueSampler(_FakeDb({}).execute, dialect=_dialect(DataSourceType.MYSQL))
        assert await sampler.sample([]) == {}


class TestCache:
    async def test_cache_hit_skips_db_call(self) -> None:
        cache: dict = {}
        db = _FakeDb({"STATUS": ["1", "A"]})
        sampler1 = ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.MYSQL), cache=cache)
        cls = _cls("X", "T", _prop("status", "STRING", "STATUS"))
        assert await sampler1.sample([cls]) == {("T", "STATUS"): ["1", "A"]}
        sampler2 = ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.MYSQL), cache=cache)
        assert await sampler2.sample([cls]) == {("T", "STATUS"): ["1", "A"]}
        assert len(db.calls) == 1  # 第二次命中缓存，未再访问 DB

    async def test_cache_keyed_by_datasource(self) -> None:
        cache: dict = {}
        db = _FakeDb({"STATUS": ["1"]})
        cls = _cls("X", "T", _prop("status", "STRING", "STATUS"))
        sampler1 = ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.MYSQL), cache=cache)
        await sampler1.sample([cls])
        sampler2 = ValueSampler(db.execute, datasourceId=2, dialect=_dialect(DataSourceType.MYSQL), cache=cache)
        await sampler2.sample([cls])
        assert len(db.calls) == 2  # 不同数据源重新采样

    async def test_module_cache_clear(self) -> None:
        clearValueSampleCache()
        db = _FakeDb({"STATUS": ["1"]})
        cls = _cls("X", "T", _prop("status", "STRING", "STATUS"))
        await ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.MYSQL)).sample([cls])
        assert len(db.calls) == 1
        await ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.MYSQL)).sample([cls])
        assert len(db.calls) == 1  # 模块缓存命中
        clearValueSampleCache()
        await ValueSampler(db.execute, datasourceId=1, dialect=_dialect(DataSourceType.MYSQL)).sample([cls])
        assert len(db.calls) == 2  # 清空后重新采样
