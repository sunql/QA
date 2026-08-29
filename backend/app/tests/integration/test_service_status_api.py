"""GET /api/v1/system/status 集成测试。

真实 PostgreSQL + 完整 API 链路：PG 探测走真实引擎（SELECT 1 → up）；
Neo4j / Milvus / Embedding 属外部依赖，monkeypatch 探测函数为确定性结果，
避免依赖真实服务。断言响应形状与 camelCase JSON 契约。
"""

from __future__ import annotations

from app.domain.schemas import ServiceStatus
from app.services import service_status_service as svc_module


async def _stubNeo4j() -> ServiceStatus:
    return ServiceStatus(name="neo4j", status="up", latency_ms=1, endpoint="bolt://localhost:7687")


async def _stubMilvus() -> ServiceStatus:
    return ServiceStatus(name="milvus", status="down", latency_ms=2, detail="milvus unreachable")


async def _stubEmbedding() -> ServiceStatus:
    return ServiceStatus(name="embedding", status="not_configured", latency_ms=3)


async def test_service_status_endpoint(client, monkeypatch) -> None:
    monkeypatch.setattr(svc_module, "_checkNeo4j", _stubNeo4j)
    monkeypatch.setattr(svc_module, "_checkMilvus", _stubMilvus)
    monkeypatch.setattr(svc_module, "_checkEmbedding", _stubEmbedding)

    resp = await client.get("/api/v1/system/status")

    assert resp.status_code == 200
    body = resp.json()
    # camelCase 契约（CamelModel alias_generator）
    assert "checkedAt" in body
    assert "services" in body
    # ⚠️ 顶层不得含 success：前端 client.ts 拦截器按 "success" in body 解包信封，
    # 一旦出现会被误解析（与 api/datasource.ts 同一约束）
    assert "success" not in body

    by_name = {s["name"]: s for s in body["services"]}
    assert set(by_name) == {"postgresql", "neo4j", "milvus", "embedding"}

    # PostgreSQL 走真实引擎，测试库可达 → up
    assert by_name["postgresql"]["status"] == "up"
    assert by_name["postgresql"]["endpoint"] is not None

    assert by_name["neo4j"]["status"] == "up"
    assert by_name["neo4j"]["latencyMs"] == 1

    assert by_name["milvus"]["status"] == "down"
    assert by_name["milvus"]["detail"] == "milvus unreachable"

    assert by_name["embedding"]["status"] == "not_configured"
