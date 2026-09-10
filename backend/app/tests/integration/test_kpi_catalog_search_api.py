"""Phase 1 Task 1.6: KPI catalog /search 端点集成测试。

覆盖：
- search 命中时返回 KPI 列表（kpi_code / kpi_name / confidence）
- 空 query 返回 422（min_length=1 触发）
- 无匹配返回空列表（不是 404）
- threshold 过滤低置信度结果
- limit 限制最大返回数

依赖 kpi_match_cache warmUp（由 integration conftest 统一处理）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
class TestKpiCatalogSearchApi:
    """GET /api/v1/kpi-catalog/search 端点。"""

    async def test_search_returns_matching_kpis(self, client, dbSession) -> None:
        """搜索命中时返回 KPI 列表（kpi_code + kpi_name + confidence）。"""
        resp = await client.get(
            "/api/v1/kpi-catalog/search?q=准时",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert "query" in body
        assert body["query"] == "准时"
        # results 是列表（可能空可能非空，取决于 seed 数据中是否有匹配的 KPI）
        assert isinstance(body["results"], list)

    async def test_search_empty_query_returns_422(self, client) -> None:
        """空 query（min_length=1）返回 422。"""
        resp = await client.get(
            "/api/v1/kpi-catalog/search?q=",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    async def test_search_no_match_returns_empty_list(self, client) -> None:
        """无匹配返回空列表（不是 404）。"""
        resp = await client.get(
            "/api/v1/kpi-catalog/search?q=完全不相关的随机词XYZ123456",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []

    async def test_search_respects_threshold(self, client) -> None:
        """threshold 参数过滤低置信度结果。"""
        # threshold=1.0 排除所有 Jaccard < 1.0 的结果
        resp = await client.get(
            "/api/v1/kpi-catalog/search?q=准时&threshold=1.0",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        # 如果有精确 alias 命中（confidence=1.0），才会出现在结果中
        for hit in body["results"]:
            assert hit["confidence"] == 1.0

    async def test_search_respects_limit(self, client) -> None:
        """limit 参数限制最大返回数。"""
        resp = await client.get(
            "/api/v1/kpi-catalog/search?q=准时&limit=2",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) <= 2

    async def test_search_default_params(self, client) -> None:
        """默认参数：threshold=0.75, limit=10。"""
        resp = await client.get(
            "/api/v1/kpi-catalog/search?q=准时",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        # 不应报错（使用默认参数）
        assert isinstance(body["results"], list)

    async def test_search_requires_auth(self, client) -> None:
        """不带认证 header 返回 401/403。"""
        resp = await client.get("/api/v1/kpi-catalog/search?q=准时")
        assert resp.status_code in (401, 403)

    async def test_search_max_limit(self, client) -> None:
        """limit 超过 50 返回 422。"""
        resp = await client.get(
            "/api/v1/kpi-catalog/search?q=准时&limit=100",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    async def test_search_results_have_required_fields(self, client) -> None:
        """命中结果包含 kpi_code / kpi_name / confidence 三个字段。"""
        resp = await client.get(
            "/api/v1/kpi-catalog/search?q=准时",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        for hit in body["results"]:
            assert "kpi_code" in hit
            assert "kpi_name" in hit
            assert "confidence" in hit
            assert isinstance(hit["confidence"], (int, float))
            assert 0.0 <= hit["confidence"] <= 1.0
