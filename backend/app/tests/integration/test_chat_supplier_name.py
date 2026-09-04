"""Chat 供应商名称解析集成测试（Phase 6.5 Task 4）。

覆盖 processMessage 入口的名字→编码预解析（真实 PG + 完整 API 链路）。
Task 5 会补全 not_found、数字回归、processMessageStream 等用例。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue

AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}

_NAME_SUPPLIER_KEY = 910505
_NAME_SUPPLIER_CODE = "910505"
_NAME_AMBIGUOUS_KEY = 910506
_NAME_AMBIGUOUS_CODE = "910506"


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        id=9301,
        name="ds-chat-name",
        type="postgresql",
        host="localhost",
        port=5432,
        database_name="x",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()
    return ds


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


async def _seedOtdFeature(dbSession: AsyncSession, code: str) -> None:
    dbSession.add(
        FeatureDefinition(
            id=9302,
            feature_name="SUPPLIER_OTD_3M",
            feature_alias="SUPPLIER_OTD_3M",
            feature_definition="auto",
            entity_type="SUPPLIER",
            calculation_logic="SELECT 1",
            window_size="3M",
            refresh_frequency=FeatureRefreshFrequency.DAILY,
            unit="%",
            status=FeatureStatus.ACTIVE,
            is_enabled=True,
            datasource_id=9301,
            version="v1.0",
        )
    )
    dbSession.add(
        FeatureValue(
            feature_id=9302,
            entity_key=code,
            value=92.5,
            valid_at=date(2026, 8, 31),
            computed_at=datetime(2026, 8, 31, tzinfo=UTC),
        )
    )
    await dbSession.commit()


@pytest.mark.asyncio
async def test_chat_name_exact_resolves_to_code(
    client: AsyncClient, dbSession: AsyncSession
):
    """精确名 → 替换为 code → 走既有 supplier_360 拦截 → 返回 360 视图。"""
    await _seedDatasource(dbSession)
    await _seedNamedSupplier(
        dbSession, _NAME_SUPPLIER_KEY, _NAME_SUPPLIER_CODE, "测试名精确供应商甲"
    )
    await _seedOtdFeature(dbSession, _NAME_SUPPLIER_CODE)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-name-1",
            "question": "供应商 测试名精确供应商甲 的 360° 视图",
            "datasourceId": 9301,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_360"
    assert _NAME_SUPPLIER_CODE in body["answer"]
    assert body["supplier360"] is not None
    assert body["supplier360"]["profile"]["enterpriseCode"] == _NAME_SUPPLIER_CODE


@pytest.mark.asyncio
async def test_chat_name_ambiguous_returns_candidates_in_answer(
    client: AsyncClient, dbSession: AsyncSession
):
    """LIKE 命中 2 条 → 200 + answer 列出候选编码（chat 惯例：不抛 422）。"""
    await _seedDatasource(dbSession)
    await _seedNamedSupplier(
        dbSession, _NAME_SUPPLIER_KEY, _NAME_SUPPLIER_CODE, "测试名歧义供应商甲"
    )
    await _seedNamedSupplier(
        dbSession, _NAME_AMBIGUOUS_KEY, _NAME_AMBIGUOUS_CODE, "测试名歧义供应商乙"
    )

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-name-2",
            "question": "供应商 测试名歧义 的 360° 视图",
            "datasourceId": 9301,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("supplier360") is None
    assert _NAME_SUPPLIER_CODE in body["answer"]
    assert _NAME_AMBIGUOUS_CODE in body["answer"]
    assert "候选" in body["answer"]


@pytest.mark.asyncio
async def test_chat_name_not_found_returns_guidance(
    client: AsyncClient, dbSession: AsyncSession
):
    """0 命中 → 200 + answer 引导用 enterprise_code 重试。"""
    await _seedDatasource(dbSession)
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-name-3",
            "question": "供应商 测试名不存在的公司 的 360° 视图",
            "datasourceId": 9301,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "10105" in body["answer"]  # 引导文案含示例编码
    assert "拼写" in body["answer"] or "重试" in body["answer"]


@pytest.mark.asyncio
async def test_chat_numeric_code_unchanged(
    client: AsyncClient, dbSession: AsyncSession
):
    """数字编码回归保护：原行为完全不变。"""
    await _seedDatasource(dbSession)
    await _seedNamedSupplier(
        dbSession, _NAME_SUPPLIER_KEY, _NAME_SUPPLIER_CODE, "测试名精确供应商甲"
    )
    await _seedOtdFeature(dbSession, _NAME_SUPPLIER_CODE)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-name-4",
            "question": f"供应商 {_NAME_SUPPLIER_CODE} 的 360° 视图",
            "datasourceId": 9301,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_360"
    assert body["supplier360"]["profile"]["enterpriseCode"] == _NAME_SUPPLIER_CODE
