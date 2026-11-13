"""matchRouteVector：向量候选只补 none、阈值双向、异常静默回落。"""

from __future__ import annotations

import pytest

from app.services import kpi_semantic_match_service as svc
from app.services.kpi_match_cache import get_kpi_match_cache

pytestmark = pytest.mark.asyncio


class _StubKpi:
    """最小 KpiCatalog 替身。

    ⚠️ 必须带 semantic_keywords：matchRoute 内部走 _phrasesOf(kpi, alias)，
    它会读 kpi.semantic_keywords（缺字段 ⇒ AttributeError）。
    """

    def __init__(
        self,
        id: int,
        kpi_code: str,
        kpi_name: str,
        metric_id: int | None = None,
        semantic_keywords: list[str] | None = None,
    ):
        self.id = id
        self.kpi_code = kpi_code
        self.kpi_name = kpi_name
        self.metric_id = metric_id
        self.semantic_keywords = semantic_keywords or []


@pytest.fixture()
def warmCache(monkeypatch):
    """预灌缓存：3 条 PUBLISHED KPI（其中 KPI_B 绑 metric 55）。"""
    cache = get_kpi_match_cache()
    kpis = [
        _StubKpi(101, "KPI_A", "指标A"),
        _StubKpi(102, "KPI_B", "指标B", 55),
        _StubKpi(103, "KPI_C", "指标C"),
    ]
    monkeypatch.setattr(cache, "is_warmUp", lambda: True)
    monkeypatch.setattr(cache, "getAll", lambda: list(kpis))
    return cache, kpis


@pytest.fixture()
def fakeVectorSearch(monkeypatch):
    """fake 向量检索：按 typeFilter 返回预设命中。"""
    state = {"kpi": [], "metric": [], "raise": False}
    calls: list[str] = []

    def _fakeSearch(query_embedding, top_k, typeFilter):
        calls.append(typeFilter)
        if state["raise"]:
            raise RuntimeError("milvus down")
        return list(state[typeFilter])[:top_k]

    class _FakeEmbeddingSvc:
        @staticmethod
        async def generateEmbedding(text: str):
            return [0.1, 0.2]

    monkeypatch.setattr(svc, "EmbeddingService", _FakeEmbeddingSvc, raising=False)
    monkeypatch.setattr(svc, "searchEmbeddingsByTypeRouted", _fakeSearch, raising=False)
    return state, calls


async def test_containment_wins_vector_not_called(warmCache, fakeVectorSearch):
    """包含式命中 ⇒ 向量检索根本不发起（反向守卫）。"""
    _, _ = warmCache
    state, calls = fakeVectorSearch
    state["kpi"] = [{"ontology_id": "101", "distance": 0.1}]

    route = await svc.getKpiSemanticMatchService().matchRouteVector("我的指标A是多少")

    assert route.kind == "hit"
    assert route.matches[0].method == "containment"
    assert calls == []


async def test_single_vector_hit(warmCache, fakeVectorSearch):
    """1 个候选 ⇒ hit + method=='vector' + distance 带值。"""
    state, _ = fakeVectorSearch
    state["kpi"] = [{"ontology_id": "101", "distance": 0.32}]

    route = await svc.getKpiSemanticMatchService().matchRouteVector("完全不沾边的问句")

    assert route.kind == "hit"
    assert route.matches[0].code == "KPI_A"
    assert route.matches[0].method == "vector"
    assert route.matches[0].distance == 0.32


async def test_metric_hit_reverse_binds_kpi(warmCache, fakeVectorSearch):
    """metric 向量命中 → kpi.metric_id 反查 → 归到绑定 KPI（KPI_B 绑 metric 55）。"""
    state, _ = fakeVectorSearch
    state["metric"] = [{"ontology_id": "55", "distance": 0.28}]

    route = await svc.getKpiSemanticMatchService().matchRouteVector("完全不沾边的问句")

    assert route.kind == "hit"
    assert route.matches[0].code == "KPI_B"
    assert route.matches[0].method == "vector"
    assert route.matches[0].distance == 0.28


async def test_over_threshold_dropped(warmCache, fakeVectorSearch):
    """双向反例：distance > KPI_VECTOR_DISTANCE_MAX ⇒ 丢弃 ⇒ none。"""
    state, _ = fakeVectorSearch
    state["kpi"] = [{"ontology_id": "101", "distance": svc.KPI_VECTOR_DISTANCE_MAX + 0.01}]

    route = await svc.getKpiSemanticMatchService().matchRouteVector("完全不沾边的问句")

    assert route.kind == "none"


async def test_two_hits_ambiguous(warmCache, fakeVectorSearch):
    """2 个候选 ⇒ ambiguous + 按 code 升序（确定性）。"""
    state, _ = fakeVectorSearch
    state["kpi"] = [
        {"ontology_id": "103", "distance": 0.30},
        {"ontology_id": "101", "distance": 0.35},
    ]

    route = await svc.getKpiSemanticMatchService().matchRouteVector("完全不沾边的问句")

    assert route.kind == "ambiguous"
    assert len(route.matches) == 2
    codes = [m.code for m in route.matches]
    assert codes == sorted(codes)
    assert codes == ["KPI_A", "KPI_C"]


async def test_milvus_failure_silent_fallback(warmCache, fakeVectorSearch):
    """Milvus 异常 ⇒ 静默回落纯文本（不抛）。"""
    state, _ = fakeVectorSearch
    state["raise"] = True

    route = await svc.getKpiSemanticMatchService().matchRouteVector("完全不沾边的问句")

    assert route.kind == "none"


async def test_cache_cold_returns_none(fakeVectorSearch, monkeypatch):
    """缓存冷 ⇒ none（不触发向量检索）。"""
    cache = get_kpi_match_cache()
    monkeypatch.setattr(cache, "is_warmUp", lambda: False)
    _, calls = fakeVectorSearch

    route = await svc.getKpiSemanticMatchService().matchRouteVector("完全不沾边的问句")

    assert route.kind == "none"
    assert calls == []
