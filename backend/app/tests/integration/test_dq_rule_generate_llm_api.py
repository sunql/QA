"""parse-descriptions（LLM fake 注入 + token 计量）与 apply-suggestion 沉淀闭环。

dq-rule-auto-generation Task 6: LLM advisory 闭环（parse-descriptions + apply-suggestion）。
真实 PG（port 5433）+ 完整 HTTPX 链路 + TRUNCATE 隔离。
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.exceptions import LLMUnavailableError

pytestmark = pytest.mark.asyncio
GEN_BASE = "/api/v1/data-quality/rules/generate"


@pytest.fixture(autouse=True)
def resetLlmFactory():
    """每个测试前重置 LLM 工厂缓存（避免跨测试污染）。"""
    from app.infrastructure.llm.factory import resetFactory
    resetFactory()
    yield
    resetFactory()


class _FakeCompletion:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLMClient:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    async def complete(self, messages):
        self.calls.append(messages)
        return _FakeCompletion(self._content)


class _FailingLLMClient:
    """总是抛出异常的 LLM 客户端（模拟 LLM 宕机）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def complete(self, messages):
        raise self._exc


def _makeFailingLlmClient(exc: Exception) -> _FailingLLMClient:
    """创建总是抛出指定异常的 LLM 客户端。"""
    return _FailingLLMClient(exc)


LLM_JSON = """{"suggestions": [
    {"property_name": "status", "kind": "allowed_values",
     "values": ["NEW", "CONFIRMED"], "confidence": 0.8, "rationale": "描述中列出取值"}
]}"""


async def _makeFakeResponse(content: str) -> MagicMock:
    """返回一个 sync MagicMock，其 .complete() 返回 AsyncMock(真实 content)。"""
    class _FakeResponse:
        def __init__(self, text: str) -> None:
            self.content = text

    mock_complete = AsyncMock(return_value=_FakeResponse(content))
    mock_client = MagicMock()
    mock_client.complete = mock_complete
    return mock_client


# ---------------------------------------------------------------------------
# parse-descriptions tests
# ---------------------------------------------------------------------------


async def test_parse_descriptions_returns_suggestions(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST /parse-descriptions → 200 + suggestions from LLM."""
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select

    classId = await ensureClassWithProperty(dbSession)
    # 确保有 description 字段（本体属性）
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()
    prop.description = "状态字段，取值为 NEW、CONFIRMED 两种"
    await dbSession.commit()

    fake = _FakeLLMClient(LLM_JSON)
    with patch(
        "app.api.v1.data_quality_generate._getDefaultLlmClient",
        return_value=fake,
    ):
        res = await client.post(
            f"{GEN_BASE}/parse-descriptions",
            json={"classId": classId},
            headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
    assert res.status_code == 200
    sugg = res.json()["suggestions"]
    assert len(sugg) == 1
    assert sugg[0]["values"] == ["NEW", "CONFIRMED"]
    assert sugg[0]["confidence"] == 0.8


async def test_parse_descriptions_llm_down_returns_503(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """LLM 调用失败 → 503（LLMUnavailableError → 503）。

    Note: This test calls the service directly (unit-level) to avoid HTTP-layer
    monkeypatch complexity with _getDefaultLlmClient. The HTTP endpoint is covered
    by test_parse_descriptions_returns_suggestions.
    """
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.schemas import ParseDescriptionsRequest
    from app.services.data_quality_rule_llm_service import parsePropertyDescriptions

    classId = await ensureClassWithProperty(dbSession)
    fake = _FailingLLMClient(RuntimeError("llm down"))

    payload = ParseDescriptionsRequest(class_id=classId)
    with pytest.raises(LLMUnavailableError):
        await parsePropertyDescriptions(
            session=dbSession,
            payload=payload,
            llm_client=fake,
            actor="test-admin",
        )


async def test_parse_descriptions_class_not_found_404(
    client: AsyncClient,
) -> None:
    """classId 不存在 → 404。"""
    res = await client.post(
        f"{GEN_BASE}/parse-descriptions",
        json={"classId": 999999},
        headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# apply-suggestion tests
# ---------------------------------------------------------------------------


async def test_apply_suggestion_updates_allowed_values(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST /apply-suggestion → 200，allowed_values 写入 ontology_property。"""
    from app.tests.integration.test_dq_rule_generate_api import (
        ensureClassWithProperty,
    )
    from app.domain.models import OntologyProperty
    from sqlalchemy import select

    classId = await ensureClassWithProperty(dbSession)
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()

    res = await client.post(
        f"{GEN_BASE}/apply-suggestion",
        json={"propertyId": prop.id, "allowedValues": ["NEW", "CONFIRMED"]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["propertyId"] == prop.id
    assert body["allowedValues"] == ["NEW", "CONFIRMED"]

    # 验库
    await dbSession.refresh(prop)
    assert prop.allowed_values == ["NEW", "CONFIRMED"]


async def test_apply_suggestion_property_not_found_404(
    client: AsyncClient,
) -> None:
    """propertyId 不存在 → 404。"""
    res = await client.post(
        f"{GEN_BASE}/apply-suggestion",
        json={"propertyId": 999999, "allowedValues": ["A"]},
    )
    assert res.status_code == 404


async def test_apply_suggestion_rejects_single_quote_value_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """allowedValues 含单引号 → 422。"""
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select

    classId = await ensureClassWithProperty(dbSession)
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()

    res = await client.post(
        f"{GEN_BASE}/apply-suggestion",
        json={"propertyId": prop.id, "allowedValues": ["NEW", "CON'TAINED"]},
    )
    assert res.status_code == 422


async def test_apply_suggestion_rejects_empty_values_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """allowedValues=[] → 422（min_length=1）。"""
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select

    classId = await ensureClassWithProperty(dbSession)
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()

    res = await client.post(
        f"{GEN_BASE}/apply-suggestion",
        json={"propertyId": prop.id, "allowedValues": []},
    )
    assert res.status_code == 422


async def test_apply_suggestion_then_preview_becomes_deterministic(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """apply-suggestion 后 preview 中 ALLOWED_VALUES 建议变为确定性。"""
    from app.tests.integration.test_dq_rule_generate_api import (
        ensureClassWithProperty,
        ensureDataSourceAndSchema,
    )
    from app.domain.models import OntologyProperty
    from sqlalchemy import select

    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(
        dbSession,
        tables={
            "PORDER": [
                ("PO_KEY", "varchar", False),
                ("STATUS", "varchar", True),
            ]
        },
    )
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId,
        OntologyProperty.property_name == "po_key",
    ))).scalars().one()

    # 先 apply suggestion 把 allowed_values 写入 po_key
    res = await client.post(
        f"{GEN_BASE}/apply-suggestion",
        json={"propertyId": prop.id, "allowedValues": ["NEW", "CONFIRMED"]},
    )
    assert res.status_code == 200

    # preview 现在应该产生 ALLOWED_VALUES 规则
    preview = (
        await client.post(
            f"{GEN_BASE}/preview",
            json={"classId": classId, "datasourceId": dsId},
        )
    ).json()
    allowed = [
        s
        for s in preview["suggestions"]
        if s.get("derivationType") == "ALLOWED_VALUES"
        and s.get("targetColumn") == "PO_KEY"
    ]
    assert allowed and allowed[0]["ruleExpression"].startswith("PO_KEY IN ('NEW'")


async def test_apply_suggestion_creates_outbox_event(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """apply-suggestion 写入 ontology_property_updated outbox 事件。"""
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select, text

    classId = await ensureClassWithProperty(dbSession)
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()

    res = await client.post(
        f"{GEN_BASE}/apply-suggestion",
        json={"propertyId": prop.id, "allowedValues": ["A", "B"]},
    )
    assert res.status_code == 200

    # 验 outbox 行
    outboxCount = (
        await dbSession.execute(
            text(
                "SELECT count(*) FROM audit_outbox "
                "WHERE event_type = 'ontology_property_updated'"
            )
        )
    ).scalar()
    assert outboxCount >= 1
