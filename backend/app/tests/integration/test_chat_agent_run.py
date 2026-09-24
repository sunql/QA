"""Chat 集成 Agent 运行时端到端测试（Phase 6.4）。

端到端覆盖 chat_service.processMessage 拦截 AGENT_RUN 意图后：
- 完整路径：seed Agent + supplier + features → 问「用 supplier_risk_agent 评估供应商 100001」
  → ChatResponse 含 agent_run（tool=supplier_risk + 等级）
- 未知 Agent：问「用 unknown_agent 帮我看看」→ answer=Agent 不存在 + agent_run=None
- 不可运行 Agent（DRAFT）：→ answer=不可运行 + agent_run=None
- 普通查询不被 agent_run 吸走：「供应商 100001 的订单数」→ intent != agent_run
- supplier_risk 问法不被 agent_run 吸走：「供应商 100001 的风险」→ intent=supplier_risk

agent_run 意图在 classifyResult 中最优先（显式指名胜过一切启发式），
因此上述最后两条本质是「正则不误吸」护栏。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import (
    AgentPermission,
    AgentResponseLatency,
    AgentStatus,
    AgentTriggerType,
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import (
    DataSource,
    EntityMapping,
    FeatureDefinition,
    FeatureValue,
    LlmConfig,
    SessionTokenUsage,
)
from app.domain.schemas import AgentAccessPolicyCreate, AgentDefinitionCreate
from app.infrastructure.llm.base_client import StreamChunk
from app.services.agent_registry_service import AgentRegistryService

AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}
_ADMIN = CurrentUser(userId="test-admin", roles=("admin",))

# ChatRequest.datasourceId 必填 int；与 supplier_risk/360 测试错开
_CHAT_DATASOURCE_ID = 9601


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        id=_CHAT_DATASOURCE_ID,
        name="ds-chat-agent-run",
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


async def _seedAgent(
    dbSession: AsyncSession,
    code: str,
    *,
    status: AgentStatus = AgentStatus.ACTIVE,
    policies: list[AgentAccessPolicyCreate] | None = None,
) -> None:
    service = AgentRegistryService()
    dto = AgentDefinitionCreate(
        agent_code=code,
        agent_name=f"{code} Chat 集成",
        description="Phase 6.4 Chat 集成测试",
        trigger_type=AgentTriggerType.USER_QUESTION,
        response_latency=AgentResponseLatency.REALTIME,
        data_domains=["PROCUREMENT"],
        # supplier_risk 工具要求 data_layers 含 DIM（写时跨字段校验）
        data_layers=["DIM", "FEATURE"],
        status=status,
        version="v1.0",
        # feat-agent-tool-binding：tool_name DB 化后运行时必查 agent_definition.tool_name，
        # 缺省 → no-tool ConflictError（409）
        tool_name="supplier_risk",
        policies=policies
        if policies is not None
        else [
            AgentAccessPolicyCreate(
                data_object="SUPPLIER",
                permission=AgentPermission.READ,
                data_layer=None,
                notes="Chat 集成测试",
            )
        ],
    )
    await service.createAgent(dbSession, dto, _ADMIN)


async def _seedSupplier(dbSession: AsyncSession, key: int, code: str) -> None:
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
        )
    )
    await dbSession.commit()


async def _seedFeatureAndValue(
    dbSession: AsyncSession,
    feature_id: int,
    feature_name: str,
    code: str,
    value: float,
    *,
    unit: str = "%",
    window: str = "3M",
) -> None:
    dbSession.add(
        FeatureDefinition(
            id=feature_id,
            feature_name=feature_name,
            feature_alias=feature_name,
            feature_definition="auto",
            entity_type="SUPPLIER",
            calculation_logic="SELECT 1",
            window_size=window,
            refresh_frequency=FeatureRefreshFrequency.DAILY,
            unit=unit,
            status=FeatureStatus.ACTIVE,
            is_enabled=True,
            datasource_id=_CHAT_DATASOURCE_ID,
            version="v1.0",
        )
    )
    dbSession.add(
        FeatureValue(
            feature_id=feature_id,
            entity_key=code,
            value=value,
            valid_at=date(2026, 8, 31),
            computed_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
    )
    await dbSession.commit()


# ---------------------------------------------------------------------------
# Chat fakes（与 test_chat_supplier_risk 一致，供普通查询用例防误吸）
# ---------------------------------------------------------------------------


class _StubEmbedding:
    async def embed(self, text):  # noqa: ARG002
        return [0.0] * 8

    async def storeQueryEmbedding(self, **kwargs):  # noqa: ARG002
        return None

    async def searchSimilarQueries(self, *args, **kwargs):  # noqa: ARG002
        return []


class _FakeAdapter:
    async def execute_read_only(self, sql):  # noqa: ARG002
        return []


class _NoopLlm:
    """按 prompt 阶段返回最小可用响应（兼容 LlmMessage 对象与 dict 两种消息形态）。"""

    @staticmethod
    def _content(msg) -> str:
        if isinstance(msg, dict):
            return msg.get("content", "")
        return getattr(msg, "content", "")

    async def complete(self, messages, **kwargs):  # noqa: ARG002
        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 1
            completionTokens = 1

        user = self._content(messages[1])
        system = self._content(messages[0])
        if "图表类型" in user:
            _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1]}]}'
        elif "解析为查询计划" in system:
            _Resp.content = (
                '{"target":"x","selectedClasses":[],'
                '"selectedProperties":[],"groupBy":[]}'
            )
        elif "生成 SQL 时必须" in system:
            _Resp.content = "```sql\nSELECT 1 AS c\n```"
        else:
            _Resp.content = "ok"
        return _Resp()

    async def completeStream(self, messages: list, **kwargs):
        """回答流（G4 中置信建议卡片测试走 _streamQuery）：两段增量 + 末块统计。"""
        for piece in ("查询完成，", "结果如下。"):
            yield StreamChunk(
                content=piece, isDone=False, promptTokens=0, completionTokens=0, modelName="test-model"
            )
        yield StreamChunk(
            content="", isDone=True, promptTokens=1, completionTokens=1, modelName="test-model"
        )


class _RouterForConfig:
    def __init__(self, config) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx):  # noqa: ARG002
        return self._config

    def selectFallbackModel(self, configs, excludeId):  # noqa: ARG002
        return None


async def _setupChatFakes(monkeypatch, dbSession: AsyncSession) -> None:
    existing = (await dbSession.execute(select(LlmConfig))).first()
    if existing:
        config = existing[0]
    else:
        config = LlmConfig(
            model_name="test-model",
            provider="openai",
            cost_per_1k_input=Decimal("0.001"),
            cost_per_1k_output=Decimal("0.002"),
        )
        dbSession.add(config)
        await dbSession.commit()
        await dbSession.refresh(config)
    import app.api.v1.chat as chatModule

    monkeypatch.setattr(chatModule._service, "_modelRouter", _RouterForConfig(config))
    monkeypatch.setattr(chatModule._service, "_llmFactory", lambda cfg: _NoopLlm())
    monkeypatch.setattr(chatModule._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chatModule._service, "_embedding", _StubEmbedding())


def _parseFrames(text: str) -> list[tuple[str, dict]]:
    """解析 SSE 帧（与 test_chat_stream_api 同口径）：event: X / data: {json}。"""
    frames: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event: str | None = None
        data: dict | None = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        frames.append((event or "", data or {}))
    return frames


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_agent_run_intent_returns_payload(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """完整路径：seed Agent + supplier + feature → 指名调用 → agent_run 含工具结果。

    monkeypatch chat fakes：让 chat 注入的 llm_factory 返回 _NoopLlm，
    supplier_risk 的 risk_points 走 LLM 生成路径（riskPointsSource=llm）。
    """
    await _seedDatasource(dbSession)
    await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50,
        unit="score", window="12M",
    )
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-1",
            "question": "用 supplier_risk_agent 评估供应商 100001",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "agent_run"
    assert "SUP000001" in body["answer"]
    run = body["agentRun"]
    assert run is not None
    assert run["agentCode"] == "SUPPLIER_RISK_AGENT"
    assert run["tool"] == "supplier_risk"
    assert run["result"]["level"] == "high"
    assert run["result"]["levelSource"] == "supplier_risk_score_main"  # 规则路径契约
    # chat 注入真实 llm_factory（_NoopLlm）→ risk_points 由 LLM 生成
    assert run["result"]["riskPointsSource"] == "llm"


@pytest.mark.asyncio
async def test_chat_agent_run_unknown_agent_friendly(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """未注册 Agent → 200 + answer=Agent 不存在（通用消息）+ agent_run=None。"""
    await _seedDatasource(dbSession)
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-2",
            "question": "用 unknown_agent 帮我看看",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "agent_run"
    assert body["agentRun"] is None
    assert "UNKNOWN_AGENT" in body["answer"]
    assert "不存在" in body["answer"]


@pytest.mark.asyncio
async def test_chat_agent_run_draft_agent_friendly(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """DRAFT 状态 Agent（真实绑定 code）→ 200 + answer=不可运行 + agent_run=None。"""
    await _seedDatasource(dbSession)
    await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT", status=AgentStatus.DRAFT)
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-3",
            "question": "用 supplier_risk_agent 评估供应商 100001",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "agent_run"
    assert body["agentRun"] is None
    assert "不可运行" in body["answer"]


@pytest.mark.asyncio
async def test_chat_agent_run_bad_input_friendly(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """arg_extractor 解析失败 → 200 + answer 引导（提供企业编码）+ agent_run=None。"""
    await _seedDatasource(dbSession)
    await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-4",
            "question": "用 supplier_risk_agent 看看今天天气",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "agent_run"
    assert body["agentRun"] is None
    assert "编码" in body["answer"]


@pytest.mark.asyncio
async def test_chat_normal_query_not_absorbed_by_agent_run(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """普通查询不含 _AGENT 指名 → 不被 agent_run 吸走。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-5",
            "question": "供应商 100001 的订单数",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] != "agent_run"
    assert body.get("agentRun") is None


@pytest.mark.asyncio
async def test_chat_supplier_risk_not_absorbed_by_agent_run(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """「供应商 100001 的风险」无 _AGENT 指名 → intent 仍为 supplier_risk。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50,
        unit="score", window="12M",
    )

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-6",
            "question": "供应商 100001 的风险",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_risk"
    assert body["supplierRisk"] is not None
    assert body.get("agentRun") is None


@pytest.mark.asyncio
async def test_chat_agent_run_stream_routes_agent_run_and_audits_tokens(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """#207 审查 HIGH 修复：AGENT_RUN 在默认流式 UI（/chat/stream）下必须可用。

    SSE 事件序列：meta(intent=agent_run) → token(整段 answer) → done(携带 agentRun 卡片)。
    另断言 done 后 token_usage 落 audit 行（审查 MEDIUM#2：Agent 内部 LLM 计量补审计）。
    """
    await _seedDatasource(dbSession)
    await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50,
        unit="score", window="12M",
    )
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat/stream",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-stream-1",
            "question": "用 supplier_risk_agent 评估供应商 100001",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parseFrames(resp.text)
    events = [e for e, _ in frames]
    assert events[0] == "meta"
    assert frames[0][1]["intent"] == "agent_run"
    # token 事件：整段 answer（含供应商编码）
    tokenFrames = [f for f in frames if f[0] == "token"]
    assert tokenFrames, f"缺 token 帧: {frames}"
    assert "SUP000001" in tokenFrames[0][1]["content"]
    # done 事件：携带 agentRun 卡片对象（前端 MessageItem 依赖此回填）
    doneFrames = [f for f in frames if f[0] == "done"]
    assert len(doneFrames) == 1
    run = doneFrames[0][1].get("agentRun")
    assert run is not None, f"done 帧缺 agentRun: {doneFrames}"
    assert run["agentCode"] == "SUPPLIER_RISK_AGENT"
    assert run["tool"] == "supplier_risk"
    assert run["result"]["level"] == "high"

    # MEDIUM#2：Agent 内部 LLM 调用补写 token_usage 审计（purpose=agent_run）
    rows = (await dbSession.execute(
        select(SessionTokenUsage).where(
            SessionTokenUsage.session_id == "test-agent-run-stream-1"
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].purpose == "agent_run"
    assert rows[0].prompt_tokens == 1
    assert rows[0].completion_tokens == 1
    assert rows[0].total_tokens == 2  # _NoopLlm prompt=1 + completion=1
    assert rows[0].model_name == "test-model"


@pytest.mark.asyncio
async def test_chat_supplier_risk_stream_routes_card(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """#207 审查 HIGH 附注：供应商风险拦截意图在流式路径同样路由（_streamInterceptCard 共享）。

    默认 UI 全走 /chat/stream；此前 supplier_risk 卡片只在非流式响应回填。
    """
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50,
        unit="score", window="12M",
    )
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat/stream",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-stream-2",
            "question": "供应商 100001 的风险",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    frames = _parseFrames(resp.text)
    assert frames[0][0] == "meta"
    assert frames[0][1]["intent"] == "supplier_risk"
    doneFrames = [f for f in frames if f[0] == "done"]
    assert len(doneFrames) == 1
    risk = doneFrames[0][1].get("supplierRisk")
    assert risk is not None, f"done 帧缺 supplierRisk: {doneFrames}"
    assert risk["level"] == "high"
    assert doneFrames[0][1].get("agentRun") is None


# ---------------------------------------------------------------------------
# Phase 7 G4：未指名 Agent 语义路由（增量：不覆盖既有意图）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_semantic_routing_high_confidence_auto_runs_agent(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """「供应商 100001 是否可靠合规」→ 高置信（≥0.7）自动调度 SUPPLIER_RISK_AGENT。

    与显式指名不同，本条不含 _AGENT token；由语义路由在既有确定性拦截
    （360/risk/graph）均未命中后，经关键词评分提升为 AGENT_RUN。
    """
    await _seedDatasource(dbSession)
    await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50,
        unit="score", window="12M",
    )
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-g4-1",
            "question": "供应商 100001 是否可靠合规",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "agent_run"
    run = body["agentRun"]
    assert run is not None, f"agent_run 卡片缺失: {body}"
    assert run["agentCode"] == "SUPPLIER_RISK_AGENT"
    assert run["tool"] == "supplier_risk"
    # 语义路由高置信路径不会附带建议卡片（已直接调度）
    assert body.get("suggestedAgent") is None


@pytest.mark.asyncio
async def test_chat_semantic_routing_medium_confidence_suggests_card(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """「供应商 100001 表现怎么样」→ 中置信（0.4）仍走 NL2SQL + suggestedAgent 卡片。

    建议卡片字段（recommendedAgentCode / confidence / reason）随响应透传，
    前端按字段存在性渲染。
    """
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-g4-2",
            "question": "供应商 100001 表现怎么样",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 中置信不覆盖 NL2SQL：仍是查询意图
    assert body["intent"] in ("query", "new_query")
    assert body.get("agentRun") is None
    suggestion = body.get("suggestedAgent")
    assert suggestion is not None, f"suggestedAgent 卡片缺失: {body}"
    assert suggestion["recommendedAgentCode"] == "SUPPLIER_360_AGENT"
    assert 0.4 <= suggestion["confidence"] < 0.7
    assert suggestion["reason"]


@pytest.mark.asyncio
async def test_chat_semantic_routing_low_confidence_no_card(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """「供应商 100001 的联系人」→ 无 Agent 关键词 → 无建议卡片（低置信护栏）。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-g4-3",
            "question": "供应商 100001 的联系人",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] in ("query", "new_query")
    assert body.get("agentRun") is None
    assert body.get("suggestedAgent") is None


@pytest.mark.asyncio
async def test_chat_semantic_routing_aggregation_not_hijacked(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """「供应商 100001 关联的采购订单总金额」→ 聚合量词兜底，纯 NL2SQL 无卡片。

    即使问法含「关联」（GRAPH_REASONING_AGENT 关键词），金额量词使其判为
    数值型聚合查询，语义路由被门禁拒绝，NL2SQL 不被吸走。
    """
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-g4-4",
            "question": "供应商 100001 关联的采购订单总金额是多少",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] in ("query", "new_query")
    assert body.get("agentRun") is None
    assert body.get("suggestedAgent") is None


@pytest.mark.asyncio
async def test_chat_semantic_routing_stream_done_carries_card(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """中置信语义路由建议卡片随 /chat/stream done 帧透传（默认 UI 全走流式）。

    前端依赖 done 帧的 suggestedAgent 字段存在性渲染 SuggestedAgentCard。
    """
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat/stream",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-agent-run-g4-stream-1",
            "question": "供应商 100001 表现怎么样",
            "datasourceId": _CHAT_DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _parseFrames(resp.text)
    assert frames[0][0] == "meta"
    assert frames[0][1]["intent"] in ("query", "new_query")
    doneFrames = [f for f in frames if f[0] == "done"]
    assert len(doneFrames) == 1
    suggestion = doneFrames[0][1].get("suggestedAgent")
    assert suggestion is not None, f"done 帧缺 suggestedAgent: {doneFrames}"
    assert suggestion["recommendedAgentCode"] == "SUPPLIER_360_AGENT"
    assert 0.4 <= suggestion["confidence"] < 0.7
