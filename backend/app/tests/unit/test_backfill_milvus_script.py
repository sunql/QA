"""回填脚本 4-collection 排雷：cleanup 不再清空 metric / KPI 向量。

背景：milvus.rebuildOntologyCollections() drop 4 个集合，而 cleanup 的 expected
只含 class/property ⇒ 跑一次 --cleanup 就清空 metric 向量（既有雷）与 KPI 向量
（Task 1 引入的新雷），脚本却照样打印「已收敛」。

⚠️ 本模块**不连真 Milvus**：所有 Milvus 入口都 monkeypatch 掉。
⚠️ import 路径必须是 ``scripts.backfill_milvus_embeddings``（包内导入）——
用 ``sys.path.insert`` + ``import backfill_milvus_embeddings`` 会把模块以**第二个
名字**导入，与 ``scripts.*`` 不是同一个模块对象 ⇒ monkeypatch 失效。
"""

from __future__ import annotations

import pytest

from scripts import backfill_milvus_embeddings as bf


def test_read_all_rows_covers_four_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    """读取面必须含 kpi collection（修复前只读 3 个）。"""
    queried: list[str] = []

    class _FakeCollection:
        def __init__(self, name: str) -> None:
            self.name = name

        def load(self, **kwargs) -> None:
            pass

        def query(self, **kwargs) -> list[dict]:
            queried.append(self.name)
            return []

    monkeypatch.setattr(
        bf.milvus,
        "_ensureCollection",
        lambda name, fields: _FakeCollection(name),
    )

    bf._readAllRowsWithVectors()

    assert queried == [
        "ontology_class_embeddings",
        "ontology_property_embeddings",
        "ontology_metric_embeddings",
        "kpi_catalog_embeddings",
    ]


def test_dedupe_keeps_metric_and_kpi_rows() -> None:
    """去重后 metric / KPI 行必须还在（修复前被 expected 过滤掉 ⇒ 向量被清空）。"""
    rows = [
        {"id": 1, "ontology_id": 10, "type": "class", "name": "C", "alias": "", "description": "", "embedding": [0.1]},
        {"id": 2, "ontology_id": 20, "type": "property", "name": "P", "alias": "", "description": "", "embedding": [0.2]},
        {"id": 3, "ontology_id": 30, "type": "metric", "name": "M", "alias": "", "description": "", "embedding": [0.3]},
        {"id": 4, "ontology_id": 40, "type": "kpi", "name": "K", "alias": "", "description": "", "embedding": [0.4]},
        {"id": 5, "ontology_id": 99, "type": "class", "name": "STALE", "alias": "", "description": "", "embedding": [0.5]},
    ]
    expectedKeys = {
        (10, "class"), (20, "property"), (30, "metric"), (40, "kpi"),
    }

    deduped = bf._dedupeRows(rows, expectedKeys)

    assert set(deduped) == expectedKeys
    assert deduped[(30, "metric")]["name"] == "M"
    assert deduped[(40, "kpi")]["name"] == "K"


def test_dedupe_keeps_latest_row_per_key() -> None:
    """同一 (oid,type) 多行时保留 id 最大的一行（重复向量清理语义）。"""
    rows = [
        {"id": 7, "ontology_id": 30, "type": "metric", "name": "OLD", "alias": "", "description": "", "embedding": [0.1]},
        {"id": 9, "ontology_id": 30, "type": "metric", "name": "NEW", "alias": "", "description": "", "embedding": [0.2]},
        {"id": 8, "ontology_id": 30, "type": "metric", "name": "MID", "alias": "", "description": "", "embedding": [0.3]},
    ]

    deduped = bf._dedupeRows(rows, {(30, "metric")})

    assert len(deduped) == 1
    assert deduped[(30, "metric")]["name"] == "NEW"


def test_write_back_records_preserve_external_id() -> None:
    """写回必须带 external_id：KPI 固定 kpi_code；本体行沿用旧值 → entityMaps → ""。"""
    deduped = {
        (10, "class"): {"ontology_id": 10, "type": "class", "name": "C", "alias": "",
                        "description": "", "embedding": [0.1], "external_id": "obj:class:10"},
        (20, "property"): {"ontology_id": 20, "type": "property", "name": "P", "alias": "",
                           "description": "", "embedding": [0.2], "external_id": ""},
        (30, "metric"): {"ontology_id": 30, "type": "metric", "name": "M", "alias": "",
                         "description": "", "embedding": [0.3], "external_id": ""},
        (40, "kpi"): {"ontology_id": 40, "type": "kpi", "name": "K", "alias": "",
                      "description": "", "embedding": [0.4], "external_id": ""},
    }
    detailsByKey = {
        (40, "kpi"): {"id": 40, "name": "K", "alias": "", "description": "",
                      "kpi_code": "KPI_CODE_40"},
    }
    entityMaps = {"class": {}, "property": {10: "obj:class:10"}, "metric": {30: "obj:metric:30"}}

    records = bf._buildWriteBackRecords(deduped, detailsByKey, entityMaps)
    byType = {r["type"]: r for r in records}

    assert set(byType) == {"class", "property", "metric", "kpi"}
    assert byType["kpi"]["external_id"] == "KPI_CODE_40"
    assert byType["class"]["external_id"] == "obj:class:10"     # 旧行保留值优先
    assert byType["property"]["external_id"] == ""              # 旧行为空、entityMaps 无 20 ⇒ 兜底
    assert byType["metric"]["external_id"] == "obj:metric:30"   # entityMaps 回填


def test_write_back_records_carry_all_insert_columns() -> None:
    """record 字段必须与 _insertIntoNewCollection 的 data 列同序同义（含 external_id）。"""
    deduped = {
        (30, "metric"): {"ontology_id": 30, "type": "metric", "name": "M", "alias": "ma",
                         "description": "ratio", "embedding": [0.3], "external_id": ""},
    }
    detailsByKey = {
        (30, "metric"): {"id": 30, "name": "M", "alias": "ma", "description": "ratio"},
    }

    records = bf._buildWriteBackRecords(deduped, detailsByKey, {"metric": {}})

    assert len(records) == 1
    assert set(records[0]) == {
        "ontology_id", "type", "name", "alias", "description", "embedding", "external_id",
    }
    assert records[0]["ontology_id"] == 30
    assert records[0]["embedding"] == [0.3]


def test_details_triple_normalizes_missing_values() -> None:
    """_detailsTriple 与 _rowDetails 同口径：None/缺失归一为 ""。"""
    detail = {"id": 1, "name": "N", "alias": None, "description": ""}

    assert bf._detailsTriple(detail) == ("N", "", "")
    assert bf._detailsTriple({"id": 1}) == ("", "", "")


class _FakeSessionContext:
    """async with 用的极简 session 上下文（_cleanup 只把它传给 _expectedFromPg）。"""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> bool:
        return False


class _FakeSessionFactory:
    """替身 sessionmaker：调用一次返回一个 async context manager。"""

    def __call__(self) -> _FakeSessionContext:
        return _FakeSessionContext()


class _FakeEmbeddingService:
    """假 embedding 服务（不打真 LLM）。"""

    async def generateEmbedding(self, text: str) -> list[float]:
        return [0.5, 0.5]


class _FakeMetric:
    """替身 OntologyMetric（只需文本构造用到的字段）。"""

    metric_name = "M"
    metric_alias = "ma"
    agg_function = "ratio"
    formula = "SUM(x)"


class _FakeKpi:
    """替身 KpiCatalog（只需文本构造用到的字段）。"""

    kpi_name = "K"
    kpi_code = "KPI_40"
    business_definition = ""
    semantic_keywords = None
    unit = None


def _installCleanupFakes(
    monkeypatch: pytest.MonkeyPatch,
    milvusRows: list[dict],
    insertCalls: list[list[dict]],
) -> None:
    """把 `_cleanup` 的外部依赖全换成替身（PG 期望集 / Milvus / embedding）。

    Milvus 替身按真语义建模：insert 后读得到、rebuild（drop+建）清空集合。
    """
    async def _fakeExpectedFromPg(session: object) -> tuple[dict, dict, dict]:
        expected = {
            "class": [{"id": 10, "entity": object()}],
            "property": [{"id": 20, "entity": object()}],
            "metric": [{"id": 30, "entity": _FakeMetric()}],
            "kpi": [{"id": 40, "entity": _FakeKpi(), "metricAlias": ""}],
        }
        details = {
            "class": [{"id": 10, "name": "C", "alias": "", "description": ""}],
            "property": [{"id": 20, "name": "P", "alias": "", "description": ""}],
            "metric": [{"id": 30, "name": "M", "alias": "", "description": ""}],
            "kpi": [{"id": 40, "name": "K", "alias": "", "description": "",
                     "kpi_code": "KPI_40"}],
        }
        entityMaps = {"class": {}, "property": {}, "metric": {30: "obj:metric:30"}}
        return expected, details, entityMaps

    def _fakeRebuild() -> None:
        milvusRows.clear()

    def _fakeInsert(records: list[dict]) -> None:
        insertCalls.append(list(records))
        milvusRows.extend(records)

    def _fakeListAll() -> list[dict]:
        return [
            {
                "ontology_id": r["ontology_id"], "type": r["type"], "name": r["name"],
                "alias": r["alias"], "description": r["description"],
            }
            for r in milvusRows
        ]

    monkeypatch.setattr(bf, "getSessionFactory", _FakeSessionFactory)
    monkeypatch.setattr(bf, "_expectedFromPg", _fakeExpectedFromPg)
    monkeypatch.setattr(bf, "EmbeddingService", _FakeEmbeddingService)
    monkeypatch.setattr(bf, "_readAllRowsWithVectors", lambda: list(milvusRows))
    monkeypatch.setattr(bf.milvus, "rebuildOntologyCollections", _fakeRebuild)
    monkeypatch.setattr(bf.milvus, "insertEmbeddings", _fakeInsert)
    monkeypatch.setattr(bf.milvus, "listAllEmbeddings", _fakeListAll)


async def test_cleanup_writes_back_all_four_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端（fake Milvus）：--cleanup 跑一次 ⇒ 四类向量全部写回（metric/KPI 不被清空）。

    这是本任务的**验收标准**的回归测试：修复前 expected 只含 class/property ⇒
    rebuild 后 metric / KPI 向量被清空，而脚本照样打印「已收敛」。
    """
    milvusRows = [
        {"id": 1, "ontology_id": 10, "type": "class", "name": "C", "alias": "",
         "description": "", "embedding": [0.1], "external_id": ""},
        {"id": 2, "ontology_id": 20, "type": "property", "name": "P", "alias": "",
         "description": "", "embedding": [0.2], "external_id": ""},
        {"id": 3, "ontology_id": 30, "type": "metric", "name": "M", "alias": "",
         "description": "", "embedding": [0.3], "external_id": ""},
        {"id": 4, "ontology_id": 40, "type": "kpi", "name": "K", "alias": "",
         "description": "", "embedding": [0.4], "external_id": ""},
    ]
    insertCalls: list[list[dict]] = []
    _installCleanupFakes(monkeypatch, milvusRows, insertCalls)

    failed = await bf._cleanup(dryRun=False)

    assert failed == 0
    written = insertCalls[-1]
    assert {r["type"] for r in written} == {"class", "property", "metric", "kpi"}
    byType = {r["type"]: r for r in written}
    assert byType["kpi"]["external_id"] == "KPI_40"
    assert byType["metric"]["external_id"] == "obj:metric:30"   # 旧行为空 ⇒ entityMaps 回填
    assert byType["class"]["external_id"] == ""                # entityMaps 无 ⇒ 兜底
    assert {r["ontology_id"] for r in written} == {10, 20, 30, 40}


async def test_cleanup_refills_missing_metric_and_kpi_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """metric / KPI 向量缺失 ⇒ --cleanup 补齐并写回（不再被 rebuild 清空）。"""
    milvusRows = [
        {"id": 1, "ontology_id": 10, "type": "class", "name": "C", "alias": "",
         "description": "", "embedding": [0.1], "external_id": ""},
        {"id": 2, "ontology_id": 20, "type": "property", "name": "P", "alias": "",
         "description": "", "embedding": [0.2], "external_id": ""},
    ]
    insertCalls: list[list[dict]] = []
    _installCleanupFakes(monkeypatch, milvusRows, insertCalls)

    failed = await bf._cleanup(dryRun=False)

    assert failed == 0
    assert len(insertCalls) == 2                      # 补缺失一批 + rebuild 写回一批
    written = insertCalls[-1]
    assert {r["type"] for r in written} == {"class", "property", "metric", "kpi"}
    byType = {r["type"]: r for r in written}
    assert byType["kpi"]["external_id"] == "KPI_40"
    assert byType["metric"]["external_id"] == "obj:metric:30"
    assert byType["metric"]["embedding"] == [0.5, 0.5]   # 重新生成的向量
    assert byType["kpi"]["embedding"] == [0.5, 0.5]



def test_int_keyed_map_skips_non_numeric_external_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """external_id 非数字 ⇒ 该行不参与映射，但必须被计数上报（不静默吞）。"""
    raw = {"10": "obj:class:10", "20": "obj:class:20", "abc": "obj:class:xyz"}

    mapped = bf._intKeyedMap(raw, "class")

    assert mapped == {10: "obj:class:10", 20: "obj:class:20"}
    assert "abc" not in mapped
    reported = capsys.readouterr().out
    assert "1" in reported
    assert "abc" in reported
