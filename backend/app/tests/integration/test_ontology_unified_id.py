"""A4 写路径改造集成测试：unified_id 贯穿 PG → Neo4j → Milvus。

覆盖：Class / Property / Metric 创建后 id_mapping 表记录正确，
且 Neo4j upsert 调用传入 unified_id 而非整数 id。

Harness/rules/测试规范.md 强制规则：真实 PG + 完整 API 链路。
Neo4j / Milvus 通过 monkeypatch mock（外部依赖隔离）。
"""
from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Mock Neo4j & Milvus（记录调用参数，供断言）
# ---------------------------------------------------------------------------

_neo4j_calls: list[dict[str, Any]] = []


class _MockNeo4jDriver:
    class _Session:
        def __init__(self, _: object) -> None:
            pass

        def __enter__(self) -> "_MockNeo4jDriver._Session":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def run(self, cql: str, **params: dict[str, Any]) -> list[Any]:
            _neo4j_calls.append({"cql": cql[:300], "params": dict(params)})
            # 符合 neo4j.IRecord 签名的 mock
            class _Record:
                def __init__(self, data: dict) -> None:
                    self._data = data

                def __getitem__(self, key: str) -> Any:
                    return self._data[key]

            class _Nodes:
                def __init__(self, props: dict) -> None:
                    self._props = props

                def __iter__(self):
                    return iter([])

            if "hasCycle" in cql:
                return [_Record({"hasCycle": False})]
            if "RETURN count(n)" in cql:
                return [_Record({"deleted": 1})]
            if "MERGE (c:Class" in cql:
                return [_Record({"c": _Nodes({"unified_id": params.get("unified_id", params.get("id"))})})]
            if "MERGE (p:Property" in cql:
                return [_Record({"p": _Nodes({"unified_id": params.get("unified_id", params.get("id"))})})]
            if "MERGE (m:Metric" in cql:
                return [_Record({"m": _Nodes({"unified_id": params.get("unified_id", params.get("id"))})})]
            return [_Record({"n": _Nodes({})})]

    def session(self) -> _MockNeo4jDriver._Session:
        return _MockNeo4jDriver._Session(None)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def mockNeo4jAndMilvus(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局 mock：替换 Neo4j driver 和 Milvus，记录调用参数。"""
    import app.infrastructure.neo4j_client as neo4j_module
    import app.infrastructure.milvus_client as milvus_module
    import app.services.ontology_service as ontology_module

    monkeypatch.setattr(neo4j_module, "_DRIVER", _MockNeo4jDriver())
    monkeypatch.setattr(neo4j_module, "getDriver", lambda: _MockNeo4jDriver())

    # Milvus mock：记录 insertEmbeddings 调用
    _milvus_calls: list[dict[str, Any]] = []

    def _mock_insert(records):
        _milvus_calls.extend(records)

    def _mock_delete(*a, **kw):
        pass

    monkeypatch.setattr(milvus_module, "insertEmbeddings", _mock_insert)
    monkeypatch.setattr(milvus_module, "deleteByOntologyId", _mock_delete)
    monkeypatch.setattr(ontology_module.milvus, "insertEmbeddings", _mock_insert)
    monkeypatch.setattr(ontology_module.milvus, "deleteByOntologyId", _mock_delete)

    _neo4j_calls.clear()
    _milvus_calls.clear()


# ---------------------------------------------------------------------------
# Class
# ---------------------------------------------------------------------------

async def testCreateClass_produces_unified_id_in_id_mapping(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """创建 Class 后，id_mapping 表有对应行，unified_id 格式为 obj:CLASS:{pg_id}。"""
    response = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Customer", "classAlias": "客户", "sourceTable": "t_customer"},
    )
    assert response.status_code == 201
    data = response.json()
    class_id = data["id"]

    # 断言 id_mapping 表记录存在
    raw = (
        await dbSession.execute(
            text(
                "SELECT unified_id, business_object, external_id, pg_table, pg_id "
                "FROM id_mapping WHERE pg_table = 'ontology_class' AND pg_id = :pg_id"
            ),
            {"pg_id": str(class_id)},
        )
    ).one_or_none()
    assert raw is not None, "id_mapping row not found after class creation"
    unified_id, bo, ext, tbl, pg_id = raw
    assert unified_id == f"obj:CLASS:{class_id}", f"unexpected unified_id: {unified_id}"
    assert bo == "CLASS"
    assert ext == str(class_id)
    assert tbl == "ontology_class"
    assert pg_id == str(class_id)


async def testCreateClass_neo4j_receives_unified_id(
    client: AsyncClient,
) -> None:
    """Neo4j MERGE 调用传入 unified_id 而非整数 id。"""
    _neo4j_calls.clear()
    response = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Supplier", "sourceTable": "t_supplier"},
    )
    assert response.status_code == 201
    class_id = response.json()["id"]
    expected_uid = f"obj:CLASS:{class_id}"

    # 找到 upsertClassNode 对应的 CQL 调用
    upsert_calls = [
        c for c in _neo4j_calls
        if "MERGE (c:Class" in c["cql"]
    ]
    assert len(upsert_calls) >= 1, f"no MERGE Class call found: {_neo4j_calls}"
    params = upsert_calls[0]["params"]
    assert "unified_id" in params, f"unified_id not in params: {params}"
    assert params["unified_id"] == expected_uid, (
        f"Neo4j received id={params.get('id')!r}, expected unified_id={expected_uid}"
    )


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

async def testCreateProperty_produces_unified_id_in_id_mapping(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """创建 Property 后，id_mapping 表有对应行，unified_id 格式为 obj:PROPERTY:{pg_id}。"""
    # 先创建 Class（作为 Property 的宿主）
    class_resp = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Product", "sourceTable": "t_product"},
    )
    class_id = class_resp.json()["id"]

    prop_resp = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": class_id,
            "propertyName": "productCode",
            "propertyAlias": "产品编码",
            "dataType": "VARCHAR",
            "sourceColumn": "code",
        },
    )
    assert prop_resp.status_code == 201
    prop_id = prop_resp.json()["id"]

    raw = (
        await dbSession.execute(
            text(
                "SELECT unified_id, business_object, external_id "
                "FROM id_mapping WHERE pg_table = 'ontology_property' AND pg_id = :pg_id"
            ),
            {"pg_id": str(prop_id)},
        )
    ).one_or_none()
    assert raw is not None, "id_mapping row not found after property creation"
    unified_id, bo, ext = raw
    assert unified_id == f"obj:PROPERTY:{prop_id}", f"unexpected unified_id: {unified_id}"
    assert bo == "PROPERTY"


async def testCreateProperty_neo4j_receives_unified_id(
    client: AsyncClient,
) -> None:
    """Neo4j MERGE 调用传入 unified_id 而非整数 id。"""
    _neo4j_calls.clear()
    class_resp = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Order", "sourceTable": "t_order"},
    )
    class_id = class_resp.json()["id"]

    prop_resp = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": class_id,
            "propertyName": "orderAmount",
            "dataType": "DECIMAL",
            "sourceColumn": "amount",
        },
    )
    assert prop_resp.status_code == 201
    prop_id = prop_resp.json()["id"]
    expected_uid = f"obj:PROPERTY:{prop_id}"

    upsert_calls = [
        c for c in _neo4j_calls
        if "MERGE (p:Property" in c["cql"]
    ]
    assert len(upsert_calls) >= 1, f"no MERGE Property call found: {_neo4j_calls}"
    params = upsert_calls[0]["params"]
    assert "unified_id" in params, f"unified_id not in params: {params}"
    assert params["unified_id"] == expected_uid


# ---------------------------------------------------------------------------
# Metric
# ---------------------------------------------------------------------------

async def testCreateMetric_produces_unified_id_in_id_mapping(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """创建 Metric 后，id_mapping 表有对应行，unified_id 格式为 obj:METRIC:{pg_id}。"""
    metric_resp = await client.post(
        "/api/v1/ontology/metrics",
        json={
            "metricName": "totalSales",
            "metricAlias": "总销售额",
            "formula": "SUM(amount)",
            "aggFunction": "SUM",
        },
    )
    assert metric_resp.status_code == 201
    metric_id = metric_resp.json()["id"]

    raw = (
        await dbSession.execute(
            text(
                "SELECT unified_id, business_object, external_id "
                "FROM id_mapping WHERE pg_table = 'ontology_metric' AND pg_id = :pg_id"
            ),
            {"pg_id": str(metric_id)},
        )
    ).one_or_none()
    assert raw is not None, "id_mapping row not found after metric creation"
    unified_id, bo, ext = raw
    assert unified_id == f"obj:METRIC:{metric_id}", f"unexpected unified_id: {unified_id}"
    assert bo == "METRIC"


async def testCreateMetric_neo4j_receives_unified_id(
    client: AsyncClient,
) -> None:
    """Neo4j MERGE 调用传入 unified_id 而非整数 id。"""
    _neo4j_calls.clear()
    metric_resp = await client.post(
        "/api/v1/ontology/metrics",
        json={
            "metricName": "avgPrice",
            "metricAlias": "平均价格",
            "formula": "AVG(price)",
            "aggFunction": "AVG",
        },
    )
    assert metric_resp.status_code == 201
    metric_id = metric_resp.json()["id"]
    expected_uid = f"obj:METRIC:{metric_id}"

    upsert_calls = [
        c for c in _neo4j_calls
        if "MERGE (m:Metric" in c["cql"]
    ]
    assert len(upsert_calls) >= 1, f"no MERGE Metric call found: {_neo4j_calls}"
    params = upsert_calls[0]["params"]
    assert "unified_id" in params, f"unified_id not in params: {params}"
    assert params["unified_id"] == expected_uid
