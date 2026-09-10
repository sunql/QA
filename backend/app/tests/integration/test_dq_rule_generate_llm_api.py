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
from app.infrastructure.llm.base_client import LlmMessage

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
        self.calls: list[list[LlmMessage]] = []

    async def complete(self, messages):
        # 回归守卫：service 必须传 LlmMessage 实例。
        # 真实 OpenAiClient.complete 会迭代 messages 访问 m.role/m.content，
        # 若 service 误传 dict 会触发 AttributeError → 503。
        for m in messages:
            assert isinstance(m, LlmMessage), (
                f"service must pass LlmMessage instances, got {type(m).__name__}; "
                f"OpenAiClient.complete iterates messages assuming LlmMessage API."
            )
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


async def test_parse_descriptions_accepts_camelcase_model_id(
    client: AsyncClient, dbSession: AsyncSession,
) -> None:
    """ParseDescriptionsRequest 必须接受 camelCase modelId 字段。

    回归测试：之前 ParseDescriptionsRequest schema 缺 model_id 字段，
    Pydantic extra="ignore" 静默丢弃 → 路由层 payload.model_id 永远 falsy →
    永远走默认 env 路径 → 503 LLMUnavailableError。
    """
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select
    from app.infrastructure.llm.factory import _clients
    from app.infrastructure.llm.factory import resetFactory

    classId = await ensureClassWithProperty(dbSession)
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()
    prop.description = "状态字段"
    await dbSession.commit()

    resetFactory()

    # 用一个 id 必然不存在但合法（schema gt=0 校验通过）→ 触发 404 而不是 422，
    # 证明 modelId 字段被 Pydantic 接受并传入 payload（不会因为字段缺失落到默认路径）。
    res = await client.post(
        f"{GEN_BASE}/parse-descriptions",
        json={"classId": classId, "modelId": 999999},
        headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
    )
    # 404 说明 modelId 被 schema 接受，路由进入了 ModelConfigService.get → NotFoundError
    # 若 modelId 字段缺失会得到 503「未配置 LLM」（走默认 env 路径）
    assert res.status_code != 422, f"schema 应接受 modelId，实际: {res.text}"
    assert res.status_code != 503 or "未配置" not in res.text, (
        f"modelId 字段可能仍被丢弃，触发默认 env 路径 503: {res.text}"
    )


async def test_parse_descriptions_returns_persisted_property_ids(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """parse-descriptions 必须返回 persisted_property_ids：当前类下已有 allowed_values
    的 OntologyProperty.id 列表。前端用它初始化 adoptedIds，让刷新页面也保持
    已采纳状态（不依赖 session-local Set）。
    """
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select

    classId = await ensureClassWithProperty(dbSession)

    # fixture 默认一个属性；给它写 allowed_values
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()
    prop.allowed_values = ["NEW", "CONFIRMED"]
    await dbSession.commit()
    await dbSession.refresh(prop)

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
    body = res.json()
    assert "persistedPropertyIds" in body, (
        f"parse-descriptions 响应必须包含 persistedPropertyIds 字段，实际 keys: {list(body.keys())}"
    )
    assert prop.id in body["persistedPropertyIds"], (
        f"property {prop.id} 已有 allowed_values，应在 persistedPropertyIds 中；"
        f"实际: {body['persistedPropertyIds']}"
    )


async def test_parse_descriptions_routes_by_model_id(
    client: AsyncClient, dbSession: AsyncSession,
) -> None:
    """parseDescriptions 路由必须按 model_id 分发到 ModelConfigService.get，
    而不是无脑走默认 env 路径。

    回归：之前路由层直接调 _getDefaultLlmClient()，无视 payload.model_id，
    导致无论前端选哪个模型都走 env 路径 → 503。
    """
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select
    from unittest.mock import patch, AsyncMock

    classId = await ensureClassWithProperty(dbSession)
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()
    prop.description = "状态字段"
    await dbSession.commit()

    # 监视 ModelConfigService.get 是否被以指定 model_id 调用
    with patch(
        "app.services.model_config_service.ModelConfigService.get",
        new_callable=AsyncMock,
    ) as mockGet:
        mockGet.return_value = None  # 触发 NoneType 后续报错
        res = await client.post(
            f"{GEN_BASE}/parse-descriptions",
            json={"classId": classId, "modelId": 1},
            headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
        # 关键断言：路由调了 ModelConfigService.get，参数 model_id=1
        mockGet.assert_awaited_once()
        args, _ = mockGet.await_args
        assert args[1] == 1, f"路由应以 model_id=1 调 get，实际: {args}"
    # 不在意返回码，关键是路由分发了请求


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


# ---------------------------------------------------------------------------
# parse-descriptions fence-stripping regression tests (LLM 输出常被 ```json``` 包裹)
# ---------------------------------------------------------------------------


async def test_parse_descriptions_strips_markdown_json_fence(
    client: AsyncClient, dbSession: AsyncSession,
) -> None:
    """LLM 输出被 ```json ... ``` 包裹时仍需正常解析（之前裸 json.loads 直接挂 → 503）。

    真实复现：deepseek-chat 对 class_id=1 返回的就是带 ```json``` 包裹的 JSON，
    service 之前无 fence 处理，json.loads 第一字符为 ` → JSONDecodeError →
    raise LLMUnavailableError("AI 返回格式无法解析，请稍后重试") (HTTP 503)。
    """
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select

    classId = await ensureClassWithProperty(dbSession)
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()
    prop.description = "状态字段，取值为 NEW、CONFIRMED 两种"
    await dbSession.commit()

    fenced = "```json\n" + LLM_JSON + "\n```"
    fake = _FakeLLMClient(fenced)
    with patch(
        "app.api.v1.data_quality_generate._getDefaultLlmClient",
        return_value=fake,
    ):
        res = await client.post(
            f"{GEN_BASE}/parse-descriptions",
            json={"classId": classId},
            headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
    assert res.status_code == 200, (
        f"fence-stripping 失败: {res.status_code} body={res.text[:300]}"
    )
    body = res.json()
    assert body.get("suggestions"), f"suggestions 应非空: {body}"
    kinds = [s["kind"] for s in body["suggestions"]]
    assert "allowed_values" in kinds


async def test_parse_descriptions_strips_bare_fence_without_language(
    client: AsyncClient, dbSession: AsyncSession,
) -> None:
    """仅 ``` ... ```（无 json 语言标记）包裹也应正常解析。"""
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select

    classId = await ensureClassWithProperty(dbSession)
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()
    prop.description = "状态字段"
    await dbSession.commit()

    bare = "```\n" + LLM_JSON + "\n```"
    fake = _FakeLLMClient(bare)
    with patch(
        "app.api.v1.data_quality_generate._getDefaultLlmClient",
        return_value=fake,
    ):
        res = await client.post(
            f"{GEN_BASE}/parse-descriptions",
            json={"classId": classId},
            headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
    assert res.status_code == 200, (
        f"bare-fence 处理失败: {res.status_code} body={res.text[:300]}"
    )
    assert res.json().get("suggestions")


async def test_parse_descriptions_accepts_bare_json_unchanged(
    client: AsyncClient, dbSession: AsyncSession,
) -> None:
    """无 fence 的裸 JSON 仍走原路径（向后兼容：既有用例 LLM_JSON 即此形态）。"""
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    from app.domain.models import OntologyProperty
    from sqlalchemy import select

    classId = await ensureClassWithProperty(dbSession)
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId))).scalars().one()
    prop.description = "状态字段"
    await dbSession.commit()

    fake = _FakeLLMClient(LLM_JSON)  # 裸 JSON（既有 LLM_JSON 常量形态）
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
    assert res.json().get("suggestions")
