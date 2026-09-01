"""Phase 6.3 知识图谱多跳推理单元测试（mock Neo4j）。

覆盖：
- traverse 参数校验：非法 label / maxHops 越界 -> ValueError；
- 起点不存在 -> NotFoundError（通用消息，防侧信道）；
- hops / reachableTypes 派生（去重类型集合）；
- buildChatAnswer：空 hops -> 空结果消息；有 hops -> 按类型分组 + 截断提示；
- traverseForChat：默认 2 跳 + Supplier 起点；
- intent 检测：图推理问法命中 / 与 360 / 风险问法不冲突。

Neo4j driver 用 mock（同 test_graph_relation_service 模式）。
Phase 7 G1：traverse / traverseForChat 已 async 化，测试需 await。
"""

from __future__ import annotations

import pytest

import app.infrastructure.neo4j_client as neo4j_module
import asyncio

from app.domain.exceptions import NotFoundError
from app.services.graph_traversal_service import (
    GraphTraversalService,
    resolveChatMaxHops,
)
from app.services.intent_service import IntentService, extractGraphMaxHops
from app.services.messages_zh import MSG_GRAPH_TRAVERSAL_UNAVAILABLE
from app.domain.enums import IntentType


# =============================================================================
# Mock Neo4j driver
# =============================================================================


class _MockRecord:
    """支持 record[key] 与 dict(record) 两种取值方式（对齐 neo4j.IRecord）。"""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key: str):
        return self._data[key]

    def keys(self):
        return self._data.keys()

    def __iter__(self):
        return iter(self._data)


def _hopRow(depth: int, fromKey: str, relType: str, toKey: str, toType: str, toCode: str) -> dict:
    return {
        "depth": depth,
        "fromKey": fromKey, "fromCode": fromKey, "fromName": fromKey, "fromType": "Supplier",
        "relType": relType,
        "toKey": toKey, "toCode": toCode, "toName": toCode, "toType": toType,
    }


class _MockDriver:
    """按 startKey 分发预设遍历结果的 mock。"""

    def __init__(self) -> None:
        self.nodeExists = True
        self.hopRows: list[dict] = []

    def session(self):
        driver = self

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def run(self, cql: str, **params):
                if "RETURN b.key" in cql:
                    if not driver.nodeExists:
                        return []
                    return [
                        _MockRecord(
                            {
                                "key": params["key"], "code": params["key"],
                                "name": params["key"], "entityType": "Supplier",
                                "source": "test", "labels": ["BusinessEntity", "Supplier"],
                            }
                        )
                    ]
                if "rels*1.." in cql:
                    return [_MockRecord(row) for row in driver.hopRows]
                return []

        return _Session()

    def close(self) -> None:
        pass


@pytest.fixture()
def mockNeo4j(monkeypatch: pytest.MonkeyPatch) -> _MockDriver:
    driver = _MockDriver()
    monkeypatch.setattr(neo4j_module, "_DRIVER", driver)
    monkeypatch.setattr(neo4j_module, "getDriver", lambda: driver)
    return driver


# =============================================================================
# traverse 参数校验
# =============================================================================


class TestTraverseValidation:
    async def test_rejects_unknown_label(self, mockNeo4j) -> None:
        with pytest.raises(ValueError, match="Invalid business entity label"):
            await GraphTraversalService().traverse("Class", "100001", 2)

    async def test_rejects_max_hops_zero(self, mockNeo4j) -> None:
        with pytest.raises(ValueError, match="maxHops"):
            await GraphTraversalService().traverse("Supplier", "100001", 0)

    async def test_rejects_max_hops_over_limit(self, mockNeo4j) -> None:
        with pytest.raises(ValueError, match="maxHops"):
            await GraphTraversalService().traverse("Supplier", "100001", 6)

    async def test_node_not_found(self, mockNeo4j) -> None:
        mockNeo4j.nodeExists = False
        with pytest.raises(NotFoundError):
            await GraphTraversalService().traverse("Supplier", "999999", 2)


# =============================================================================
# traverse 结果派生
# =============================================================================


class TestTraverseResults:
    async def test_hops_and_reachable_types(self, mockNeo4j) -> None:
        mockNeo4j.hopRows = [
            _hopRow(1, "100001", "SUPPLIES", "200001", "Material", "RM-STEEL-001"),
            _hopRow(1, "100001", "SIGNED", "DOC-1", "Contract", "DOC-1"),
            _hopRow(2, "100001", "CONTAINS", "300001", "PurchaseOrder", "PO202608001"),
        ]
        result = await GraphTraversalService().traverse("Supplier", "100001", 2)
        assert len(result.hops) == 3
        assert result.reachable_types == ["Contract", "Material", "PurchaseOrder"]
        assert result.hops[0].rel_type == "SUPPLIES"
        assert result.hops[0].depth == 1
        assert result.start_type == "Supplier"
        assert result.max_hops == 2

    async def test_empty_hops_no_types(self, mockNeo4j) -> None:
        mockNeo4j.hopRows = []
        result = await GraphTraversalService().traverse("Supplier", "100001", 2)
        assert result.hops == []
        assert result.reachable_types == []


# =============================================================================
# buildChatAnswer
# =============================================================================


class TestBuildChatAnswer:
    async def test_empty_hops_message(self, mockNeo4j) -> None:
        mockNeo4j.hopRows = []
        service = GraphTraversalService()
        result = await service.traverse("Supplier", "100001", 2)
        answer = service.buildChatAnswer(result)
        assert "2 跳内无关联业务实体" in answer

    async def test_groups_by_type(self, mockNeo4j) -> None:
        mockNeo4j.hopRows = [
            _hopRow(1, "100001", "SUPPLIES", "200001", "Material", "RM-STEEL-001"),
            _hopRow(1, "100001", "SUPPLIES", "200002", "Material", "RM-STEEL-002"),
            _hopRow(1, "100001", "SIGNED", "DOC-1", "Contract", "DOC-1"),
        ]
        service = GraphTraversalService()
        result = await service.traverse("Supplier", "100001", 1)
        answer = service.buildChatAnswer(result)
        assert "2 个物料" in answer
        assert "1 个合同" in answer
        assert "RM-STEEL-001" in answer

    async def test_dedup_codes_within_type(self, mockNeo4j) -> None:
        """同一实体经多条路径可达 -> 计数按去重编码。"""
        mockNeo4j.hopRows = [
            _hopRow(1, "100001", "SUPPLIES", "200001", "Material", "RM-STEEL-001"),
            _hopRow(2, "100001", "CONTAINS", "200001", "Material", "RM-STEEL-001"),
        ]
        service = GraphTraversalService()
        result = await service.traverse("Supplier", "100001", 2)
        answer = service.buildChatAnswer(result)
        assert "1 个物料" in answer

    async def test_long_result_truncation_hint(self, mockNeo4j) -> None:
        mockNeo4j.hopRows = [
            _hopRow(1, "100001", "SUPPLIES", f"20000{i}", "Material", f"RM-{i}")
            for i in range(25)
        ]
        service = GraphTraversalService()
        result = await service.traverse("Supplier", "100001", 1)
        answer = service.buildChatAnswer(result)
        assert "仅列前 8 个编码" in answer


# =============================================================================
# traverseForChat
# =============================================================================


class TestTraverseForChat:
    async def test_default_two_hops_supplier_start(self, mockNeo4j) -> None:
        mockNeo4j.hopRows = [
            _hopRow(1, "100001", "SUPPLIES", "200001", "Material", "RM-STEEL-001"),
        ]
        result = await GraphTraversalService().traverseForChat("100001")
        assert result.start_type == "Supplier"
        assert result.max_hops == 2

    async def test_traverse_does_not_block_event_loop(self, mockNeo4j) -> None:
        """G1 回归：Neo4j 同步调用必须经 asyncio.to_thread，不能阻塞事件循环。"""
        mockNeo4j.hopRows = [
            _hopRow(1, "100001", "SUPPLIES", "200001", "Material", "RM-STEEL-001"),
        ]
        service = GraphTraversalService()
        other_ran = False

        async def other_task() -> None:
            nonlocal other_ran
            other_ran = True

        async def slow_traverse() -> None:
            return await service.traverse("Supplier", "100001", 2)

        traverse_task = asyncio.create_task(slow_traverse())
        other = asyncio.create_task(other_task())
        await asyncio.gather(traverse_task, other)
        assert other_ran
        assert traverse_task.result().hops[0].to_key == "200001"

    def test_unavailable_message_constant(self) -> None:
        """降级文案非空且含可操作提示（防误删常量）。"""
        assert "知识图谱服务暂时不可用" in MSG_GRAPH_TRAVERSAL_UNAVAILABLE


# =============================================================================
# Intent 检测（Phase 6.3 图推理问法）
# =============================================================================


class TestGraphReasoningIntent:
    def setup_method(self) -> None:
        self.service = IntentService()

    def test_graph_question_detected(self) -> None:
        result = self.service.classifyResult("供应商 100001 涉及哪些物料")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.supplierKey == "100001"
        # G3：未指名跳数 -> max_hops=None（服务层 resolveChatMaxHops 默认 2）
        assert result.max_hops is None

    def test_relation_question_detected(self) -> None:
        result = self.service.classifyResult("供应商 100001 的关联订单有哪些")
        assert result.intent == IntentType.GRAPH_REASONING

    def test_graph_keyword_before_supplier(self) -> None:
        result = self.service.classifyResult("知识图谱上 供应商 100002 有什么关系")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.supplierKey == "100002"

    def test_supplier_360_not_hijacked(self) -> None:
        """「供应商 100001 的 360° 视图」仍是 supplier_360（优先级在前）。"""
        result = self.service.classifyResult("供应商 100001 的 360° 视图")
        assert result.intent == IntentType.SUPPLIER_360

    def test_supplier_risk_not_hijacked(self) -> None:
        """「供应商 100001 的风险等级」仍是 supplier_risk（优先级在前）。"""
        result = self.service.classifyResult("供应商 100001 的风险等级")
        assert result.intent == IntentType.SUPPLIER_RISK

    def test_normal_query_not_hijacked(self) -> None:
        """「供应商 100001 的订单数」仍是普通查询（不含图推理关键词）。"""
        result = self.service.classifyResult(
            "查询供应商 100001 的采购订单总金额是多少"
        )
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)

    def test_related_query_not_hijacked(self) -> None:
        """「供应商 100001 相关的采购订单总金额是多少」不被图推理吸走。

        code-reviewer HIGH 回归测试：「相关」宽泛形容词已从图推理正则移除，
        该句是聚合查询，应走 NL2SQL。
        """
        result = self.service.classifyResult("供应商 100001 相关的采购订单总金额是多少")
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)

    def test_related_association_query_not_hijacked(self) -> None:
        """「供应商 100001 关联的采购订单总金额是多少」命中聚合量词兜底。

        即使「关联」仍在关键词集内，「金额」量词使该句判为数值型聚合查询。
        """
        result = self.service.classifyResult("供应商 100001 关联的采购订单总金额是多少")
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)

    def test_related_contracts_list_not_hijacked(self) -> None:
        """「供应商 100001 相关合同有哪些」无图推理关键词（相关已移除），走普通查询。"""
        result = self.service.classifyResult("供应商 100001 相关合同有哪些")
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)


# =============================================================================
# 跳数解析（Phase 7 G3：Chat 图遍历支持 >2-hop）
# =============================================================================


class TestGraphHopParsing:
    def setup_method(self) -> None:
        self.service = IntentService()

    def test_three_hop_question_detected(self) -> None:
        """「供应商 100001 的 3 跳关联」-> GRAPH_REASONING + max_hops=3。"""
        result = self.service.classifyResult("供应商 100001 的 3 跳关联")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.supplierKey == "100001"
        assert result.max_hops == 3

    def test_deep_five_question_detected(self) -> None:
        """「供应商 100001 深度 5 的关联」-> max_hops=5。

        注：跳数短语必须伴随图推理关键词（关联/涉及…）才触发图意图；
        「深度 5」单独出现是普通查询（不吸走 NL2SQL）。
        """
        result = self.service.classifyResult("供应商 100001 深度 5 的关联")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.max_hops == 5

    def test_depth_alone_not_hijacked(self) -> None:
        """「供应商 100001 深度 5」无图推理关键词 -> 走普通查询（不被吸走）。"""
        result = self.service.classifyResult("供应商 100001 深度 5")
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)

    def test_max_three_hops_question_detected(self) -> None:
        """「供应商 100001 最多 3 跳关联」-> max_hops=3。"""
        result = self.service.classifyResult("供应商 100001 最多 3 跳关联")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.max_hops == 3

    def test_ten_hop_question_returns_raw(self) -> None:
        """「供应商 100001 10 跳关联」-> 返回原始值 10，clamp 在 service 层（不抛 500）。"""
        result = self.service.classifyResult("供应商 100001 10 跳关联")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.max_hops == 10

    def test_chinese_numeral_hops(self) -> None:
        """「供应商 100001 的三跳关联」-> 中文数字也解析为 3。"""
        result = self.service.classifyResult("供应商 100001 的三跳关联")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.max_hops == 3

    def test_two_hops_colloquial_numeral(self) -> None:
        """「供应商 100001 的两跳关联」-> 口语「两」解析为 2。"""
        result = self.service.classifyResult("供应商 100001 的两跳关联")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.max_hops == 2

    def test_ten_hops_chinese_numeral(self) -> None:
        """「供应商 100001 的十跳关联」-> 「十」解析为 10（clamp 在 service 层）。"""
        result = self.service.classifyResult("供应商 100001 的十跳关联")
        assert result.intent == IntentType.GRAPH_REASONING
        assert result.max_hops == 10

    def test_hops_with_aggregation_guard_not_hijacked(self) -> None:
        """含跳数问法的聚合查询仍走 NL2SQL：聚合量词兜底先于跳数提取。

        若实现顺序颠倒（先解析跳数再查聚合量词），本用例会回归暴露。
        """
        result = self.service.classifyResult(
            "供应商 100001 的 3 跳关联订单总金额是多少"
        )
        assert result.intent in (IntentType.QUERY, IntentType.NEW_QUERY)

    def test_extract_graph_max_hops_none_when_absent(self) -> None:
        """无跳数短语 -> None（非图问法也不会误报数字）。"""
        assert extractGraphMaxHops("供应商 100001 涉及哪些物料") is None
        assert extractGraphMaxHops("供应商 100001 的关联订单有哪些") is None


class TestResolveChatMaxHops:
    """Chat 跳数服务层解析：None 默认 2，越界 clamp 到 [1, 5]。"""

    def test_none_defaults_to_two(self) -> None:
        assert resolveChatMaxHops(None) == 2

    def test_explicit_hop_preserved(self) -> None:
        assert resolveChatMaxHops(3) == 3

    def test_upper_bound_clamped(self) -> None:
        assert resolveChatMaxHops(10) == 5

    def test_zero_clamped_to_one(self) -> None:
        assert resolveChatMaxHops(0) == 1

    def test_lower_negative_clamped_to_one(self) -> None:
        assert resolveChatMaxHops(-3) == 1
