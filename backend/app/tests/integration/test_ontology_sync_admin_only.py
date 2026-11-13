"""ontology「同步」写面仅 admin（4 个端点）。

背景（feat-kpi-vector-graph-sync）：`POST /ontology/graph/sync-missing` 的写面
本批扩大到了 KPI 的 id_mapping 与 KpiCatalog 节点/边；`embeddings/sync-missing`
会重建向量，`classes/{id}/embedding` 与 `embeddings/sync` 会覆盖单条向量。
这些操作都会改共享基础设施（Milvus / Neo4j / id_mapping），与「读本体」不同
⇒ 收成 admin-only；读侧（list / search）仍对登录用户开放。

两条断言方向都要（防止「先干活再被拦」或「只在 UI 藏按钮」）：
1. 非 admin ⇒ 403，且 **service 一次都没被调用**；
2. admin ⇒ 正常返回，且确实走到了对应 service。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_ADMIN_HEADERS = {"X-User-Id": "admin", "X-User-Roles": "admin"}
_USER_HEADERS = {"X-User-Id": "alice", "X-User-Roles": "user"}

_EMBEDDING_SYNC_MISSING_RESULT = {
    "totalClasses": 0,
    "missingCount": 0,
    "syncedCount": 0,
    "failedCount": 0,
    "failures": [],
    "totalProperties": 0,
    "missingPropertyCount": 0,
    "syncedPropertyCount": 0,
    "failedPropertyCount": 0,
    "propertyFailures": [],
    "totalMetrics": 0,
    "missingMetricCount": 0,
    "syncedMetricCount": 0,
    "failedMetricCount": 0,
    "metricFailures": [],
}

_GRAPH_SYNC_MISSING_RESULT = {
    "totalClasses": 0,
    "missingClassCount": 0,
    "syncedClassCount": 0,
    "totalProperties": 0,
    "missingPropertyCount": 0,
    "syncedPropertyCount": 0,
    "totalJoins": 0,
    "missingJoinCount": 0,
    "syncedJoinCount": 0,
    "totalRelations": 0,
    "missingRelationCount": 0,
    "syncedRelationCount": 0,
    "failedCount": 0,
    "failures": [],
}

_EMBEDDING_SYNC_PAYLOAD = {
    "ontologyId": 990001,
    "type": "class",
    "name": "AdminGateProbe",
    "embedding": [0.0] * 1024,
}


@pytest.fixture(autouse=True)
def serviceSpy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """替换 4 个 service 入口：既不碰真实 Milvus/Neo4j，又能断言「是否真的被调用」。"""
    from app.services.ontology_service import OntologyService

    calls: list[str] = []

    async def _syncMissingClassEmbeddings(*_args: object, **_kwargs: object) -> dict:
        calls.append("syncMissingClassEmbeddings")
        return dict(_EMBEDDING_SYNC_MISSING_RESULT)

    async def _syncMissingGraph(*_args: object, **_kwargs: object) -> dict:
        calls.append("syncMissingGraph")
        return dict(_GRAPH_SYNC_MISSING_RESULT)

    async def _syncClassEmbedding(*_args: object, **_kwargs: object) -> None:
        calls.append("syncClassEmbedding")

    def _syncEmbedding(*_args: object, **_kwargs: object) -> None:
        calls.append("syncEmbedding")

    monkeypatch.setattr(
        OntologyService, "syncMissingClassEmbeddings", _syncMissingClassEmbeddings
    )
    monkeypatch.setattr(OntologyService, "syncMissingGraph", _syncMissingGraph)
    monkeypatch.setattr(OntologyService, "syncClassEmbedding", _syncClassEmbedding)
    monkeypatch.setattr(OntologyService, "syncEmbedding", _syncEmbedding)
    return calls


_SYNC_ENDPOINTS = [
    pytest.param(
        "/api/v1/ontology/embeddings/sync-missing",
        None,
        200,
        "syncMissingClassEmbeddings",
        id="embeddings-sync-missing",
    ),
    pytest.param(
        "/api/v1/ontology/graph/sync-missing",
        None,
        200,
        "syncMissingGraph",
        id="graph-sync-missing",
    ),
    pytest.param(
        "/api/v1/ontology/classes/990001/embedding",
        None,
        204,
        "syncClassEmbedding",
        id="class-embedding",
    ),
    pytest.param(
        "/api/v1/ontology/embeddings/sync",
        _EMBEDDING_SYNC_PAYLOAD,
        204,
        "syncEmbedding",
        id="embeddings-sync",
    ),
]


@pytest.mark.parametrize("url,payload,expectedStatus,expectedCall", _SYNC_ENDPOINTS)
async def test_non_admin_is_forbidden_and_service_not_called(
    client: AsyncClient,
    serviceSpy: list[str],
    url: str,
    payload: dict | None,
    expectedStatus: int,
    expectedCall: str,
) -> None:
    response = await client.post(url, json=payload, headers=_USER_HEADERS)

    assert response.status_code == 403, (
        f"{url} 非 admin 应 403，实得 {response.status_code}"
    )
    assert serviceSpy == [], (
        f"{url} 非 admin 请求必须在校验层被拦下，不得先进入 service（实际调用：{serviceSpy}）"
    )


@pytest.mark.parametrize("url,payload,expectedStatus,expectedCall", _SYNC_ENDPOINTS)
async def test_admin_reaches_service(
    client: AsyncClient,
    serviceSpy: list[str],
    url: str,
    payload: dict | None,
    expectedStatus: int,
    expectedCall: str,
) -> None:
    response = await client.post(url, json=payload, headers=_ADMIN_HEADERS)

    assert response.status_code == expectedStatus, (
        f"{url} admin 应 {expectedStatus}，实得 {response.status_code}: {response.text}"
    )
    assert serviceSpy == [expectedCall], (
        f"{url} 应只调用 {expectedCall}，实际：{serviceSpy}"
    )
