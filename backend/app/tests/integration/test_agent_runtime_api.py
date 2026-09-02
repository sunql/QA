"""Phase 6.4 Agent 运行时 REST API 集成测试（真实 PostgreSQL + 完整 API 链路）。

覆盖 ``POST /api/v1/agents/{agent_code}/run``：
- 成功：3 个绑定工具（supplier_360 / supplier_risk / graph_traverse）→ 200 + AgentRunRead
- 404：Agent 未注册
- 409：DRAFT 状态 / 无工具绑定的元数据 Agent（PROCUREMENT_COPILOT_AGENT）
- 403：FORBIDDEN 策略（deny-by-default）
- 422：arg_extractor 解析失败（输入无供应商编码）

graph_traverse 依赖真实 Neo4j（不可达时跳过，与 test_graph_traversal_api 同策略）。
供应商 / Feature 数据直接 seed 到真实 PG（与 test_chat_supplier_risk 同模式）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import (
    AgentPermission,
    AgentResponseLatency,
    AgentStatus,
    AgentTriggerType,
    EntityType,
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue
from app.domain.schemas import AgentAccessPolicyCreate, AgentDefinitionCreate
from app.services.agent_binding_cache import agent_binding_cache
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_tools import AGENT_DEFAULT_BINDINGS, agent_tool_registry
from app.infrastructure import neo4j_client as neo4j

AUTH_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
_ADMIN = CurrentUser(userId="test-admin", roles=("admin",))

_neo4jAvailable = neo4j.isNeo4jAvailable()


@pytest.fixture(autouse=True)
async def warm_binding_cache(dbSession):
    """每个测试前 warmUp cache（test_agent_tool_binding_runtime.py 风格）。"""
    agent_binding_cache.invalidate()
    await agent_binding_cache.warmUp(dbSession)
    yield
    agent_binding_cache.invalidate()


def _defaultPolicies(code: str) -> list[AgentAccessPolicyCreate]:
    """按 Agent 绑定工具的 data_object + data_layers 生成每层 READ 策略。

    显式分层（非 None 通配）→ 集成测试在真实最小权限策略上验证运行时分层判定。
    无工具绑定的元数据 Agent → 回退 SUPPLIER@READ 通配（仅展示；运行时在工具门禁 409）。
    """
    tool_name = AGENT_DEFAULT_BINDINGS.get(code)
    if tool_name is None:
        return [
            AgentAccessPolicyCreate(
                data_object="SUPPLIER",
                permission=AgentPermission.READ,
                data_layer=None,
                notes="集成测试",
            )
        ]
    tool = agent_tool_registry.get(tool_name)
    assert tool is not None, f"tool {tool_name} 未注册"
    return [
        AgentAccessPolicyCreate(
            data_object=tool.data_object,
            permission=AgentPermission.READ,
            data_layer=layer,
            notes="集成测试（显式分层）",
        )
        for layer in tool.data_layers
    ]


async def _seedAgent(
    dbSession: AsyncSession,
    code: str,
    *,
    status: AgentStatus = AgentStatus.ACTIVE,
    policies: list[AgentAccessPolicyCreate] | None = None,
) -> None:
    """用 AgentRegistryService 注册一个 Agent（走 service + 真实 PG，幂等按 code 唯一）。

    默认策略用 _defaultPolicies（显式分层），显式传 policies 则覆盖。
    tool_name 写入时从 tool.data_layers 自动推导 data_layers（满足 validator 要求）。
    """
    tool_name = AGENT_DEFAULT_BINDINGS.get(code)
    data_layers = ["FEATURE"]
    if tool_name is not None:
        tool = agent_tool_registry.get(tool_name)
        if tool is not None:
            data_layers = list(tool.data_layers)
    service = AgentRegistryService()
    dto = AgentDefinitionCreate(
        agent_code=code,
        agent_name=f"{code} 集成测试",
        description="Phase 6.4 集成测试",
        trigger_type=AgentTriggerType.USER_QUESTION,
        response_latency=AgentResponseLatency.REALTIME,
        data_domains=["PROCUREMENT"],
        data_layers=data_layers,
        status=status,
        version="v1.0",
        tool_name=tool_name,
        policies=policies if policies is not None else _defaultPolicies(code),
    )
    await service.createAgent(dbSession, dto, _ADMIN)


async def _seedSupplier(dbSession: AsyncSession, key: int, code: str) -> None:
    dbSession.add(
        EntityMapping(
            entity_type=EntityType.SUPPLIER,
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


async def _seedFeatureDatasource(dbSession: AsyncSession) -> DataSource:
    """FeatureDefinition 的 datasource_id 外键必须指向真实 DataSource 行。"""
    ds = DataSource(
        id=9501,
        name="ds-agent-run-feature",
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
            entity_type=EntityType.SUPPLIER,
            calculation_logic="SELECT 1",
            window_size=window,
            refresh_frequency=FeatureRefreshFrequency.DAILY,
            unit=unit,
            status=FeatureStatus.ACTIVE,
            is_enabled=True,
            datasource_id=9501,
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


def _run_url(code: str) -> str:
    return f"/api/v1/agents/{code}/run"


class TestRunSuccess:
    @pytest.mark.asyncio
    async def test_run_supplier_360_agent_success(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """supplier_360 工具：EntityMapping → 200 + AgentRunRead（tool/supplier360）。"""
        await _seedAgent(dbSession, "SUPPLIER_360_AGENT")
        await _seedSupplier(dbSession, 100001, "SUP000001")

        resp = await client.post(
            _run_url("SUPPLIER_360_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "供应商 100001 的 360° 视图"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agentCode"] == "SUPPLIER_360_AGENT"
        assert body["tool"] == "supplier_360"
        assert body["result"]["profile"]["enterpriseKey"] == 100001
        assert "SUP000001" in body["answer"]
        assert body["tokensUsed"] == 0
        assert body["executedAt"] is not None

    @pytest.mark.asyncio
    async def test_run_supplier_risk_agent_success(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """supplier_risk 工具：EntityMapping + RISK_SCORE → 200 + 等级判定 + Token 计量。"""
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        await _seedSupplier(dbSession, 100001, "SUP000001")
        await _seedFeatureDatasource(dbSession)
        await _seedFeatureAndValue(
            dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50,
            unit="score", window="12M",
        )

        resp = await client.post(
            _run_url("SUPPLIER_RISK_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "评估供应商 100001 的风险"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agentCode"] == "SUPPLIER_RISK_AGENT"
        assert body["tool"] == "supplier_risk"
        assert body["result"]["level"] == "high"
        assert body["result"]["levelSource"] == "risk_score"
        # llm_factory=None（REST run）→ fallback_template，无 LLM 消耗
        assert body["result"]["riskPointsSource"] == "fallback_template"
        assert body["tokensUsed"] == 0
        assert "high" in body["answer"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _neo4jAvailable, reason="Neo4j 不可达，跳过图推理集成测试")
    async def test_run_graph_reasoning_agent_success(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """graph_traverse 工具：Neo4j 业务图 → 200 + hops（依赖 6.2/6.3 seed）。"""
        from scripts.seed_entity_mapping import seedEntityMappings
        from app.services.graph_relation_service import GraphRelationService

        await _seedAgent(dbSession, "GRAPH_REASONING_AGENT")
        await seedEntityMappings(dbSession)
        service = GraphRelationService()
        await service.seedGraphRelations(dbSession)
        try:
            resp = await client.post(
                _run_url("GRAPH_REASONING_AGENT"),
                headers=AUTH_HEADERS,
                json={"input": "供应商 100001 涉及哪些物料"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["tool"] == "graph_traverse"
            assert body["result"]["startKey"] == "100001"
            assert len(body["result"]["hops"]) > 0
        finally:
            neo4j.deleteBusinessGraph()


class TestRunFailures:
    @pytest.mark.asyncio
    async def test_run_unknown_agent_404(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        resp = await client.post(
            _run_url("UNKNOWN_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "随便看看"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_run_draft_agent_409(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        await _seedAgent(dbSession, "DRAFT_AGENT", status=AgentStatus.DRAFT)
        resp = await client.post(
            _run_url("DRAFT_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "供应商 100001 的 360° 视图"},
        )
        assert resp.status_code == 409
        assert "不可运行" in resp.text

    @pytest.mark.asyncio
    async def test_run_metadata_agent_without_tool_binding_409(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """PROCUREMENT_COPILOT_AGENT 无工具绑定（AGENT_DEFAULT_BINDINGS 无此 key）→ 409。"""
        await _seedAgent(dbSession, "PROCUREMENT_COPILOT_AGENT")
        resp = await client.post(
            _run_url("PROCUREMENT_COPILOT_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "随便"},
        )
        assert resp.status_code == 409
        assert "不可运行" in resp.text

    @pytest.mark.asyncio
    async def test_run_forbidden_policy_403(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """FORBIDDEN 策略：用已绑定工具的真实 code（SUPPLIER_RISK_AGENT）注册为 FORBIDDEN。"""
        await _seedAgent(
            dbSession,
            "SUPPLIER_RISK_AGENT",
            policies=[
                AgentAccessPolicyCreate(
                    data_object="SUPPLIER", permission=AgentPermission.FORBIDDEN
                )
            ],
        )
        resp = await client.post(
            _run_url("SUPPLIER_RISK_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "评估供应商 100001"},
        )
        assert resp.status_code == 403
        assert "FORBIDDEN" in resp.text

    @pytest.mark.asyncio
    async def test_run_forbidden_overrides_wildcard_read_403(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """HIGH 缺口回归：通配 None READ 不能覆盖层级 FORBIDDEN（显式否决优先）。

        与 test_run_forbidden_policy_403 的「只有 FORBIDDEN」不同，这里 FORBIDDEN 与
        跨层 READ 并存——正是安全审查指出的可绕过配置（None 通配使分层约束形同虚设）。
        """
        await _seedAgent(
            dbSession,
            "SUPPLIER_RISK_AGENT",
            policies=[
                AgentAccessPolicyCreate(
                    data_object="SUPPLIER", permission=AgentPermission.READ, data_layer=None
                ),
                AgentAccessPolicyCreate(
                    data_object="SUPPLIER",
                    permission=AgentPermission.FORBIDDEN,
                    data_layer="FEATURE",
                ),
            ],
        )
        resp = await client.post(
            _run_url("SUPPLIER_RISK_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "评估供应商 100001"},
        )
        assert resp.status_code == 403
        assert "FORBIDDEN" in resp.text

    @pytest.mark.asyncio
    async def test_run_missing_policy_deny_by_default_403(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """deny-by-default：已绑定工具但无任何策略 → 403（数据对象不可达）。"""
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT", policies=[])
        resp = await client.post(
            _run_url("SUPPLIER_RISK_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "评估供应商 100001"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_run_layer_mismatch_403(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """分层粒度生效：supplier_risk 读 DIM+FEATURE，仅授 FEATURE（缺 DIM）→ 403 + 消息含缺失层。"""
        await _seedAgent(
            dbSession,
            "SUPPLIER_RISK_AGENT",
            policies=[
                AgentAccessPolicyCreate(
                    data_object="SUPPLIER",
                    permission=AgentPermission.READ,
                    data_layer="FEATURE",
                )
            ],
        )
        resp = await client.post(
            _run_url("SUPPLIER_RISK_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "评估供应商 100001"},
        )
        assert resp.status_code == 403
        assert "无读取策略" in resp.text
        assert "DIM" in resp.text

    @pytest.mark.asyncio
    async def test_run_bad_input_422(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """arg_extractor 解析失败（输入无供应商编码）→ 422。"""
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        resp = await client.post(
            _run_url("SUPPLIER_RISK_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": "今天天气怎么样"},
        )
        assert resp.status_code == 422
        assert "解析执行参数" in resp.text

    @pytest.mark.asyncio
    async def test_run_empty_input_422(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """请求体 input 为空字符串 → Pydantic 422（min_length=1）。"""
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        resp = await client.post(
            _run_url("SUPPLIER_RISK_AGENT"),
            headers=AUTH_HEADERS,
            json={"input": ""},
        )
        assert resp.status_code == 422
