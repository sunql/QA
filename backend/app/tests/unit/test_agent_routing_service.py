"""Phase 7 G4: 未指名 Agent 语义路由测试（feat-agent-semantic-routing）。

覆盖：
1. AgentRoutingService.route 关键词评分（计划四用例 + 中置信宽松问法）；
2. IntentService.classifyResult 集成：高置信 → AGENT_RUN；中置信 → QUERY +
   suggested_agent；低置信 → 无建议；
3. 负向护栏：既有 360/risk/graph 意图不被覆盖；聚合量词兜底不吸走 NL2SQL。

实体门禁（无供应商编码不路由）与聚合量词兜底在 intent_service 调用点施加，
route() 只做纯关键词评分（避免 service 间循环 import）。
"""

from __future__ import annotations

from app.domain.enums import IntentType
from app.services.agent_routing_service import AgentRoutingService
from app.services.intent_service import IntentService


class TestRouteScoring:
    def setup_method(self) -> None:
        self.router = AgentRoutingService()

    def test_routes_supplier_risk_for_assessment(self) -> None:
        """「评估供应商 100001 风险」→ SUPPLIER_RISK_AGENT 高置信。"""
        sug = self.router.route("评估供应商 100001 风险")
        assert sug is not None
        assert sug.recommended_agent_code == "SUPPLIER_RISK_AGENT"
        assert sug.confidence >= 0.7
        assert "风险" in sug.reason

    def test_routes_supplier_360_for_view(self) -> None:
        """「供应商 100001 的 360° 视图」→ SUPPLIER_360_AGENT。"""
        sug = self.router.route("供应商 100001 的 360° 视图")
        assert sug is not None
        assert sug.recommended_agent_code == "SUPPLIER_360_AGENT"

    def test_routes_graph_for_related_materials(self) -> None:
        """「供应商 100001 关联哪些物料」→ GRAPH_REASONING_AGENT。"""
        sug = self.router.route("供应商 100001 关联哪些物料")
        assert sug is not None
        assert sug.recommended_agent_code == "GRAPH_REASONING_AGENT"

    def test_medium_confidence_for_loose_question(self) -> None:
        """「供应商 100001 表现怎么样」→ 单关键词 → 中置信（建议卡片而非自动跑）。"""
        sug = self.router.route("供应商 100001 表现怎么样")
        assert sug is not None
        assert sug.recommended_agent_code == "SUPPLIER_360_AGENT"
        assert 0.4 <= sug.confidence < 0.7

    def test_no_route_for_generic_procurement(self) -> None:
        """「今年采购金额」→ 无 Agent 关键词 → 不路由。"""
        assert self.router.route("今年采购金额") is None


class TestIntentRoutingIntegration:
    def setup_method(self) -> None:
        self.service = IntentService()

    def test_high_confidence_auto_runs_agent(self) -> None:
        """「供应商 100001 是否可靠合规」→ AGENT_RUN(SUPPLIER_RISK_AGENT)。"""
        result = self.service.classifyResult("供应商 100001 是否可靠合规")
        assert result.intent == IntentType.AGENT_RUN
        assert result.agent_code == "SUPPLIER_RISK_AGENT"

    def test_medium_confidence_stays_query_with_suggestion(self) -> None:
        """「供应商 100001 表现怎么样」→ QUERY + suggested_agent 卡片。"""
        result = self.service.classifyResult("供应商 100001 表现怎么样")
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)
        assert result.agent_code is None
        assert result.suggested_agent is not None
        assert result.suggested_agent.recommended_agent_code == "SUPPLIER_360_AGENT"

    def test_low_confidence_no_suggestion(self) -> None:
        """「供应商 100001 的联系人」→ 无关键词 → 无建议。"""
        result = self.service.classifyResult("供应商 100001 的联系人")
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)
        assert result.suggested_agent is None

    def test_no_supplier_key_no_routing(self) -> None:
        """「帮我评估风险」→ 无供应商编码 → 实体门禁拒绝路由。"""
        result = self.service.classifyResult("帮我评估风险")
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)
        assert result.agent_code is None
        assert result.suggested_agent is None


class TestNegativeGuards:
    """NL2SQL 查询不被语义路由吸走（计划要求的负向护栏）。"""

    def setup_method(self) -> None:
        self.service = IntentService()

    def test_aggregation_query_not_hijacked(self) -> None:
        """「供应商 100001 关联的采购订单总金额」→ 聚合量词兜底 → 纯 QUERY。"""
        result = self.service.classifyResult(
            "供应商 100001 关联的采购订单总金额是多少"
        )
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)
        assert result.agent_code is None
        assert result.suggested_agent is None

    def test_existing_risk_intent_not_overridden(self) -> None:
        """「供应商 100001 的风险等级」仍是 supplier_risk（路由器不覆盖既有意图）。"""
        result = self.service.classifyResult("供应商 100001 的风险等级")
        assert result.intent == IntentType.SUPPLIER_RISK
        assert result.agent_code is None
        assert result.suggested_agent is None

    def test_existing_360_intent_not_overridden(self) -> None:
        """「供应商 100001 的 360° 视图」仍是 supplier_360。"""
        result = self.service.classifyResult("供应商 100001 的 360° 视图")
        assert result.intent == IntentType.SUPPLIER_360
        assert result.agent_code is None
        assert result.suggested_agent is None

    def test_existing_graph_intent_not_overridden(self) -> None:
        """「供应商 100001 涉及哪些物料」仍是 graph_reasoning。"""
        result = self.service.classifyResult("供应商 100001 涉及哪些物料")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.agent_code is None
        assert result.suggested_agent is None
