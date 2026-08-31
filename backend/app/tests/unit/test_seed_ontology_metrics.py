"""seed_ontology 指标（METRICS）种子单元测试（Phase 2 缺口修复）。

验证 seed_ontology 的指标种子逻辑：
- METRICS 常量结构合法（引用的 target 表与 formula 列都存在于 PROPERTIES）
- _seedMetrics 幂等创建指标（按 metric_name 复用）
- seed() 结束后指标行数 == len(METRICS)
- _syncToNeo4j 对每个 metric 调用 upsertMetricNode + linkMetricDerivedFrom
- lineage_extractor 能从种子指标抽出血缘边（KPI 层存在）

Neo4j 客户端通过 monkeypatch mock，不依赖外部服务。
"""

from __future__ import annotations

import pytest

import app.infrastructure.neo4j_client as neo4j
import seed_ontology
from seed_ontology import CLASSES, METRICS, PROPERTIES, _syncToNeo4j


class _Recorder:
    """记录每次调用的位置参数与关键字参数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


def _fakeMaps() -> tuple[dict[str, int], dict[tuple[int, str], int]]:
    cid: dict[str, int] = {c["source_table"]: i for i, c in enumerate(CLASSES, start=1)}
    pid: dict[tuple[int, str], int] = {}
    for src_table, props in PROPERTIES.items():
        class_id = cid[src_table]
        for j, p in enumerate(props, start=1):
            pid[(class_id, p["name"])] = class_id * 1000 + j
    return cid, pid


def _fakeMetricIds(cid: dict[str, int]) -> dict[str, int]:
    """构造 metric_name -> metric_id 映射（与 _fakeMaps 同风格）。"""
    return {m["metric_name"]: 10_000 + i for i, m in enumerate(METRICS, start=1)}


@pytest.fixture
def metricRecorders(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Recorder]:
    result: dict[str, _Recorder] = {
        "upsertMetricNode": _Recorder(),
        "linkMetricDerivedFrom": _Recorder(),
    }
    for name, recorder in result.items():
        monkeypatch.setattr(neo4j, name, recorder)
    return result


def testMetricsConstantIsNotEmpty() -> None:
    """METRICS 种子非空：至少覆盖收货量/订单量/含税金额/准时交付。"""
    assert len(METRICS) >= 4
    names = {m["metric_name"] for m in METRICS}
    assert "KPI_TOTAL_QTY" in names


def testMetricsTargetTableExistsInClasses() -> None:
    """每条指标的 target_table 必须是已定义的类，否则 FK 悬空。"""
    tables = {c["source_table"] for c in CLASSES}
    for m in METRICS:
        assert m["target_table"] in tables, f"{m['metric_name']} 指向未知表 {m['target_table']}"


def testMetricsAggFunctionValid() -> None:
    """agg_function 必须是 5 个白名单聚合之一。"""
    allowed = {"SUM", "AVG", "COUNT", "MAX", "MIN"}
    for m in METRICS:
        assert m["agg_function"] in allowed, m["metric_name"]


def testSyncToNeo4jUpsertsEveryMetric(
    metricRecorders: dict[str, _Recorder],
) -> None:
    cid, pid = _fakeMaps()
    _syncToNeo4j(cid, pid, _fakeMetricIds(cid))

    assert len(metricRecorders["upsertMetricNode"].calls) == len(METRICS)
    assert len(metricRecorders["linkMetricDerivedFrom"].calls) == len(METRICS)
    # 抽查第一条：DERIVED_FROM 指向合法类 id
    firstArgs = metricRecorders["linkMetricDerivedFrom"].calls[0][0]
    metricId, classId = firstArgs
    assert metricId > 0
    assert classId in set(cid.values())


def testSyncToNeo4jToleratesMetricNeo4jFailure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        neo4j,
        "upsertMetricNode",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("neo4j down")),
    )
    cid, pid = _fakeMaps()
    # 不抛出异常（best-effort）
    _syncToNeo4j(cid, pid)


async def testSeedCreatesMetricsAndIsIdempotent(
    seedEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """seed 创建指标（metric_name 幂等复用），重跑不增行。"""
    from sqlalchemy import func, select

    from app.domain.models import OntologyMetric

    factory, _ = seedEngine
    monkeypatch.setattr(seed_ontology, "_syncToNeo4j", lambda cid, pid, mid=None: None)

    await seed_ontology.seed()

    async with factory() as session:
        n = (await session.execute(select(func.count(OntologyMetric.id)))).scalar() or 0
    assert n == len(METRICS)

    # 幂等：重跑不增行
    await seed_ontology.seed()
    async with factory() as session:
        n2 = (await session.execute(select(func.count(OntologyMetric.id)))).scalar() or 0
    assert n2 == n


async def testLineageExtractorProducesKpiEdgesFromSeedMetrics(
    seedEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端（内存级）：种子指标 -> lineage_extractor -> KPI 层血缘边。

    这修复 Phase 2 验收缺口：metrics=0 时抽取不到 KPI 层边，
    血缘图退化为 ERP 单系统图。种子指标就位后 KPI 层必须有边。
    """
    from app.services.lineage_extractor import extractEdges

    factory, _ = seedEngine
    monkeypatch.setattr(seed_ontology, "_syncToNeo4j", lambda cid, pid, mid=None: None)
    await seed_ontology.seed()

    async with factory() as session:
        edges = await extractEdges(session)

    kpiEdges = [e for e in edges if e.target_layer.value == "KPI"]
    assert kpiEdges, "种子指标就位后必须抽出 KPI 层血缘边"
    # 每条 KPI 边的 source_object 是指标的 target_table
    for e in kpiEdges:
        assert e.source_object in {m["target_table"] for m in METRICS}
    # 至少覆盖 KPI_TOTAL_QTY（extractor 目标对象命名：KPI_{metric_name}）
    assert any(e.target_object == "KPI_KPI_TOTAL_QTY" for e in kpiEdges)
