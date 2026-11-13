"""Neo4j KpiCatalog 节点与 MEASURED_BY 边（fake driver，禁连真 Neo4j）。

所有用例通过 monkeypatch 替换 `neo4j_client.getDriver` ⇒ **绝不**触碰真实
Neo4j 实例（本地 7687 是生产实例）。
"""

from __future__ import annotations

import pytest

from app.infrastructure import neo4j_client


class _FakeSession:
    """最小 fake session：记录 (cql, params)，返回可迭代的固定结果行。

    默认结果行模拟 `deleteNode` 的 `RETURN count(n) AS deleted`（单行），
    使 deleteNode 的解包 `[record] = session.run(...)` 成立。
    """

    def __init__(self, recorder: list, result=None) -> None:
        self._recorder = recorder
        self._result = [{"deleted": 1}] if result is None else result

    def run(self, cql: str, **params):
        self._recorder.append((cql, params))
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


class _FakeDriver:
    def __init__(self, recorder: list, result=None) -> None:
        self._recorder = recorder
        self._result = result

    def session(self) -> _FakeSession:
        return _FakeSession(self._recorder, self._result)


def _patch(monkeypatch: pytest.MonkeyPatch, result=None) -> list:
    """把 getDriver 换成 fake driver，返回共享的 recorder 列表。"""
    recorder: list = []
    monkeypatch.setattr(
        neo4j_client, "getDriver", lambda: _FakeDriver(recorder, result)
    )
    return recorder


def test_kpi_catalog_label_whitelisted() -> None:
    # KpiCatalog 必须进白名单（CQL label 不可参数化）；
    # 且不得挤掉既有三个 label。
    assert "KpiCatalog" in neo4j_client._ALLOWED_LABELS
    for kept in ("Class", "Property", "Metric"):
        assert kept in neo4j_client._ALLOWED_LABELS


def test_upsert_merges_on_unified_id(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _patch(monkeypatch)
    neo4j_client.upsertKpiCatalogNode(
        unified_id="obj:KPI:1", code="KPI_OTD", name="准时交付率",
        status="PUBLISHED", owner="qa", unit="%",
    )
    cql, params = recorder[0]
    # MERGE（幂等）且合并键是 unified_id（不是 numeric id / code）
    assert "MERGE (k:KpiCatalog {unified_id: $unified_id})" in cql
    assert "CREATE" not in cql
    assert params == {
        "unified_id": "obj:KPI:1", "code": "KPI_OTD", "name": "准时交付率",
        "status": "PUBLISHED", "owner": "qa", "unit": "%",
    }


def test_upsert_keeps_node_for_non_published_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """D3 语义：向量只建 PUBLISHED，但图节点全状态都保留（属性原地更新）。"""
    recorder = _patch(monkeypatch)
    neo4j_client.upsertKpiCatalogNode(
        unified_id="obj:KPI:2", code="KPI_DRAFT", name=None,
        status="DRAFT", owner=None, unit=None,
    )
    cql, params = recorder[0]
    assert "MERGE (k:KpiCatalog {unified_id: $unified_id})" in cql
    assert params["status"] == "DRAFT"
    assert params["unified_id"] == "obj:KPI:2"


def test_link_measured_by(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _patch(monkeypatch)
    neo4j_client.linkKpiMeasuredBy("obj:KPI:1", "obj:METRIC:5")
    cql, params = recorder[0]
    assert "MATCH (k:KpiCatalog {unified_id: $kpiUid})" in cql
    # 单条 MATCH 内两个 pattern 以逗号分隔（与 linkMetricDerivedFrom 同款）
    assert "(m:Metric {unified_id: $metricUid})" in cql
    assert "MERGE (k)-[:MEASURED_BY]->(m)" in cql
    assert params == {"kpiUid": "obj:KPI:1", "metricUid": "obj:METRIC:5"}


def test_get_kpi_catalog_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _patch(
        monkeypatch,
        result=[{"uid": "obj:KPI:1"}, {"uid": "obj:KPI:2"}],
    )
    ids = neo4j_client.getKpiCatalogIds()
    assert ids == {"obj:KPI:1", "obj:KPI:2"}
    cql, _ = recorder[0]
    assert "MATCH (k:KpiCatalog)" in cql
    # id-read 口径与既有 helpers 一致：COALESCE(unified_id, toString(id))
    assert "COALESCE(k.unified_id, toString(k.id)) AS uid" in cql


def test_get_metric_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _patch(monkeypatch, result=[{"uid": "obj:METRIC:5"}])
    ids = neo4j_client.getMetricIds()
    assert ids == {"obj:METRIC:5"}
    cql, _ = recorder[0]
    assert "MATCH (m:Metric)" in cql
    assert "COALESCE(m.unified_id, toString(m.id)) AS uid" in cql


def test_delete_node_accepts_kpi_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """deleteNode 白名单放行 KpiCatalog（图节点删除靠它）。"""
    recorder = _patch(monkeypatch)
    deleted = neo4j_client.deleteNode("KpiCatalog", "obj:KPI:1")
    cql, params = recorder[0]
    assert "(n:KpiCatalog {unified_id: $unified_id})" in cql
    assert params == {"unified_id": "obj:KPI:1"}
    assert deleted == 1


def test_delete_node_rejects_unknown_label() -> None:
    """白名单守卫仍生效（KpiCatalog 之外不得放行）。"""
    with pytest.raises(ValueError, match="Invalid label"):
        neo4j_client.deleteNode("Kpi; DROP", "obj:KPI:1")
