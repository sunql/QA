"""Agent Runtime 供应商名称解析集成测试（Phase 6.5）。

覆盖 POST /api/v1/agents/{code}/run 的名字→编码预解析（真实 PG + 完整 API 链路）：
- 精确名 → 200（等价于数字编码输入）
- LIKE 歧义 → 422 + details.candidates
- 0 命中 → 422 + 引导文案
- 数字回归 → 原行为不变
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MatchRule, SourceSystem
from app.domain.models import EntityMapping

from app.tests.integration.test_agent_runtime_api import _seedAgent

AUTH_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

_RT_SUPPLIER_KEY = 920505
_RT_SUPPLIER_CODE = "920505"
_RT_AMBIGUOUS_KEY = 920506
_RT_AMBIGUOUS_CODE = "920506"


async def _seedNamedSupplier(
    dbSession: AsyncSession, key: int, code: str, name: str
) -> None:
    dbSession.add(
        EntityMapping(
            entity_type="SUPPLIER",
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.ERP,
            source_key=f"V{key}",
            source_code=f"V{key}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
            name=name,
        )
    )
    await dbSession.commit()


async def _runAgent(client: AsyncClient, inputText: str):
    return await client.post(
        "/api/v1/agents/SUPPLIER_360_AGENT/run",
        headers=AUTH_HEADERS,
        json={"input": inputText},
    )


@pytest.mark.asyncio
async def test_runtime_name_exact_returns_200(
    client: AsyncClient, dbSession: AsyncSession
):
    await _seedAgent(dbSession, "SUPPLIER_360_AGENT")
    await _seedNamedSupplier(
        dbSession, _RT_SUPPLIER_KEY, _RT_SUPPLIER_CODE, "测试运行时供应商甲"
    )
    resp = await _runAgent(client, "查询供应商 测试运行时供应商甲 的 360° 视图")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agentCode"] == "SUPPLIER_360_AGENT"
    # 解析路径：profile.enterprise_code 命中替换后的编码
    assert body["result"]["profile"]["enterpriseCode"] == _RT_SUPPLIER_CODE


@pytest.mark.asyncio
async def test_runtime_name_ambiguous_returns_422_with_candidates(
    client: AsyncClient, dbSession: AsyncSession
):
    await _seedAgent(dbSession, "SUPPLIER_360_AGENT")
    await _seedNamedSupplier(
        dbSession, _RT_SUPPLIER_KEY, _RT_SUPPLIER_CODE, "测试运行时歧义甲"
    )
    await _seedNamedSupplier(
        dbSession, _RT_AMBIGUOUS_KEY, _RT_AMBIGUOUS_CODE, "测试运行时歧义乙"
    )
    resp = await _runAgent(client, "查询供应商 测试运行时歧义 的情况")
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    candidates = body["details"]["candidates"]
    assert [c[0] for c in candidates] == [_RT_SUPPLIER_CODE, _RT_AMBIGUOUS_CODE]


@pytest.mark.asyncio
async def test_runtime_name_not_found_returns_422(
    client: AsyncClient, dbSession: AsyncSession
):
    await _seedAgent(dbSession, "SUPPLIER_360_AGENT")
    resp = await _runAgent(client, "查询供应商 测试运行时不存在 的情况")
    assert resp.status_code == 422, resp.text
    assert "未在主数据中找到" in resp.json()["error"]


@pytest.mark.asyncio
async def test_runtime_numeric_regression(
    client: AsyncClient, dbSession: AsyncSession
):
    """数字编码输入回归保护：与既有 test_agent_runtime_api 同语义。"""
    await _seedAgent(dbSession, "SUPPLIER_360_AGENT")
    await _seedNamedSupplier(
        dbSession, _RT_SUPPLIER_KEY, _RT_SUPPLIER_CODE, "测试运行时供应商甲"
    )
    resp = await _runAgent(client, f"查询供应商 {_RT_SUPPLIER_CODE} 的 360° 视图")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["profile"]["enterpriseCode"] == _RT_SUPPLIER_CODE
