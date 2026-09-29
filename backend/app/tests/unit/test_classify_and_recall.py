"""v3.1 A7 通道 1 收口：路由冻结对拍 + SemanticState 契约 + 零 LLM 守卫。

对拍集（_ROUTING_FIXTURE）：从历史测试真实模式抽取的 66 条问题
（unit/test_intent_service.py、test_intent_supplier_360.py、
test_graph_traversal_service.py、test_agent_routing_service.py 与 chat 集成测试），
每条跑 hasPriorState=False/True 两态，断言：

1. 新单入口 classifyAndRecall 的产出与旧路径 IntentService.classifyResult
   直调逐条 diff 为空（路由冻结守卫：未来改规则导致路由漂移，此测试必红）；
2. 冻结的期望意图矩阵不变（覆盖全部 13 类 IntentType）；
3. IntentResult.semanticState 恒被填充且字段搬运/置信度口径符合契约；
4. 全量对拍集跑 classifyAndRecall 的 LLM 调用数 == 0（快通道预算闸）。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.enums import ChartType, IntentType
from app.domain.schemas import SemanticState
from app.services.chat_recall import _RECALL_INTENTS, RecallMixin
from app.services.chat_service import ChatService
from app.services.intent_service import (
    RULE_CONFIDENCE_EXACT,
    RULE_CONFIDENCE_FALLBACK,
    IntentResult,
    IntentService,
)

# (问题, 无状态意图, 有状态意图) —— 期望矩阵冻结自 2026-09-29 现状分类器输出
_ROUTING_FIXTURE: list[tuple[str, IntentType, IntentType]] = [
    # ===== CHITCHAT（8）=====
    ("你好", IntentType.CHITCHAT, IntentType.CHITCHAT),
    ("你好呀", IntentType.CHITCHAT, IntentType.CHITCHAT),
    ("谢谢", IntentType.CHITCHAT, IntentType.CHITCHAT),
    ("HELP me", IntentType.CHITCHAT, IntentType.CHITCHAT),
    ("Hello", IntentType.CHITCHAT, IntentType.CHITCHAT),
    ("查", IntentType.CHITCHAT, IntentType.CHITCHAT),
    ("能做什么", IntentType.CHITCHAT, IntentType.CHITCHAT),
    ("再见", IntentType.CHITCHAT, IntentType.CHITCHAT),
    # ===== QUERY（无状态）→ NEW_QUERY/REFINE/FOLLOW_UP（有状态）（30）=====
    ("各供应商的收货数量汇总", IntentType.QUERY, IntentType.NEW_QUERY),
    ("  查询上个月的销售总额  ", IntentType.QUERY, IntentType.NEW_QUERY),
    ("用柱状图展示各供应商的销售额", IntentType.QUERY, IntentType.NEW_QUERY),
    ("画个饼图", IntentType.QUERY, IntentType.NEW_QUERY),
    ("折线图展示趋势", IntentType.QUERY, IntentType.NEW_QUERY),
    ("按地区汇总各供应商的销售额总额", IntentType.QUERY, IntentType.NEW_QUERY),
    ("每个供应商的收货数量汇总", IntentType.QUERY, IntentType.NEW_QUERY),
    ("查询今年的采购总额", IntentType.QUERY, IntentType.NEW_QUERY),
    ("查询上个月的销售情况", IntentType.QUERY, IntentType.NEW_QUERY),
    ("最高的是什么产品", IntentType.QUERY, IntentType.NEW_QUERY),
    ("销售额最高的是什么", IntentType.QUERY, IntentType.NEW_QUERY),
    ("什么是最畅销的产品", IntentType.QUERY, IntentType.NEW_QUERY),
    ("各供应商销售额的区别", IntentType.QUERY, IntentType.NEW_QUERY),
    ("最大的区别是什么", IntentType.QUERY, IntentType.NEW_QUERY),
    ("各业务线的销售额指标汇总", IntentType.QUERY, IntentType.NEW_QUERY),
    ("为什么A公司最多", IntentType.QUERY, IntentType.NEW_QUERY),
    ("4月份呢？", IntentType.QUERY, IntentType.FOLLOW_UP),
    ("/define", IntentType.QUERY, IntentType.NEW_QUERY),
    ("按数量排序", IntentType.QUERY, IntentType.REFINE),
    ("2024年的销售额是多少", IntentType.QUERY, IntentType.NEW_QUERY),
    ("今年采购金额", IntentType.QUERY, IntentType.NEW_QUERY),
    ("帮我评估风险", IntentType.QUERY, IntentType.NEW_QUERY),
    ("供应商 100001 的联系人", IntentType.QUERY, IntentType.NEW_QUERY),
    ("供应商 100001 的订单数", IntentType.QUERY, IntentType.NEW_QUERY),
    ("供应商 100001 相关的采购订单总金额是多少", IntentType.QUERY, IntentType.NEW_QUERY),
    ("供应商 100001 关联的采购订单总金额是多少", IntentType.QUERY, IntentType.NEW_QUERY),
    ("供应商 100001 深度 5", IntentType.QUERY, IntentType.NEW_QUERY),
    ("供应商 100001 的 3 跳关联订单总金额是多少", IntentType.QUERY, IntentType.NEW_QUERY),
    ("供应商 100001 表现怎么样", IntentType.QUERY, IntentType.NEW_QUERY),
    (
        "第一步统计3月份采购订单数量，输出列表，第二步统计3月份主要top10采购物料的占比，"
        "输出饼图，第三步分析这top10物料在4月份下的订单数量信息分析",
        IntentType.QUERY,
        IntentType.NEW_QUERY,
    ),
    # ===== CLARIFY（8）=====
    ("收货数量是什么意思", IntentType.CLARIFY, IntentType.CLARIFY),
    ("排序是什么意思", IntentType.CLARIFY, IntentType.CLARIFY),
    ("周转率是什么", IntentType.CLARIFY, IntentType.CLARIFY),
    ("毛利率是什么意思", IntentType.CLARIFY, IntentType.CLARIFY),
    ("解释一下毛利率", IntentType.CLARIFY, IntentType.CLARIFY),
    ("什么是毛利", IntentType.CLARIFY, IntentType.CLARIFY),
    ("毛利和净利的区别", IntentType.CLARIFY, IntentType.CLARIFY),
    ("这个指标是什么", IntentType.CLARIFY, IntentType.CLARIFY),
    # ===== DEFINE（5）=====
    ("定义指标 销售额 = SUM(order.amount)", IntentType.DEFINE, IntentType.DEFINE),
    ("新增指标：order_count = COUNT(id)", IntentType.DEFINE, IntentType.DEFINE),
    ("定义指标", IntentType.DEFINE, IntentType.DEFINE),
    ("/define 产品", IntentType.DEFINE, IntentType.DEFINE),
    ("/define 产品 alias=Product desc=销售商品", IntentType.DEFINE, IntentType.DEFINE),
    # ===== MAP（3）=====
    ("把 客户名称 映射到 客户类", IntentType.MAP, IntentType.MAP),
    ("订单金额 -> 订单", IntentType.MAP, IntentType.MAP),
    ("/map 客户ID -> 客户", IntentType.MAP, IntentType.MAP),
    # ===== METRIC（4）=====
    ("有哪些指标", IntentType.METRIC, IntentType.METRIC),
    ("查看销售额指标", IntentType.METRIC, IntentType.METRIC),
    ("/metric 销售额 = SUM(order.amount)", IntentType.METRIC, IntentType.METRIC),
    ("/metric 客单价", IntentType.METRIC, IntentType.METRIC),
    # ===== SUPPLIER_360（5）=====
    ("供应商 100001 的 360° 视图", IntentType.SUPPLIER_360, IntentType.SUPPLIER_360),
    ("供应商 100001 的 360 视图", IntentType.SUPPLIER_360, IntentType.SUPPLIER_360),
    ("supplier 100001 全貌", IntentType.SUPPLIER_360, IntentType.SUPPLIER_360),
    ("360 视图 供应商 100001", IntentType.SUPPLIER_360, IntentType.SUPPLIER_360),
    ("供应商 100001 的 360 度订单分布", IntentType.SUPPLIER_360, IntentType.SUPPLIER_360),
    # ===== SUPPLIER_RISK（2）=====
    ("供应商 100001 的风险等级", IntentType.SUPPLIER_RISK, IntentType.SUPPLIER_RISK),
    ("评估供应商 100001 风险", IntentType.SUPPLIER_RISK, IntentType.SUPPLIER_RISK),
    # ===== GRAPH_REASONING（6）=====
    ("供应商 100001 涉及哪些物料", IntentType.GRAPH_REASONING, IntentType.GRAPH_REASONING),
    ("供应商 100001 的关联订单有哪些", IntentType.GRAPH_REASONING, IntentType.GRAPH_REASONING),
    ("知识图谱上 供应商 100002 有什么关系", IntentType.GRAPH_REASONING, IntentType.GRAPH_REASONING),
    ("供应商 100001 的 3 跳关联", IntentType.GRAPH_REASONING, IntentType.GRAPH_REASONING),
    ("供应商 100001 10 跳关联", IntentType.GRAPH_REASONING, IntentType.GRAPH_REASONING),
    ("供应商 100001 的两跳关联", IntentType.GRAPH_REASONING, IntentType.GRAPH_REASONING),
    # ===== AGENT_RUN（1）=====
    ("供应商 100001 是否可靠合规", IntentType.AGENT_RUN, IntentType.AGENT_RUN),
    # ===== REFINE（仅有状态，6）=====
    ("按数量降序排序", IntentType.QUERY, IntentType.REFINE),
    ("只看前10条", IntentType.QUERY, IntentType.REFINE),
    ("筛选收货数量大于100的", IntentType.QUERY, IntentType.REFINE),
    ("把第一步的结果按金额降序排序", IntentType.QUERY, IntentType.REFINE),
    ("按指标排序", IntentType.QUERY, IntentType.REFINE),
    ("筛选按地区汇总销售额总额", IntentType.QUERY, IntentType.REFINE),
    # ===== FOLLOW_UP（仅有状态，7）=====
    ("它占了多少比例", IntentType.QUERY, IntentType.FOLLOW_UP),
    ("它为什么最多", IntentType.QUERY, IntentType.FOLLOW_UP),
    ("这个月最高的是哪个", IntentType.QUERY, IntentType.FOLLOW_UP),
    ("继续看下个月的数据", IntentType.QUERY, IntentType.FOLLOW_UP),
    ("接着看下个月的", IntentType.QUERY, IntentType.FOLLOW_UP),
    ("那去年呢", IntentType.QUERY, IntentType.FOLLOW_UP),
    ("哪4月份呢", IntentType.QUERY, IntentType.FOLLOW_UP),
]

assert len(_ROUTING_FIXTURE) >= 50, "对拍集须 ≥50 条"


class _Harness(RecallMixin):
    """纯分类对拍 harness：只需 _intent（sessionId=None 路径不触 DB/召回）。"""

    def __init__(self) -> None:
        self._intent = IntentService()


class _CountingLlmFactory:
    """LLM 工厂计数器：通道 1 快路径断言零调用。"""

    def __init__(self) -> None:
        self.callCount = 0

    def __call__(self, config):  # noqa: ANN001, ANN202 — 测试桩
        self.callCount += 1
        return None


def _buildCountingService(factory: _CountingLlmFactory) -> ChatService:
    """真实 ChatService 组合 + 计数 LLM 工厂（其余依赖本路径不触达，置空对象）。"""
    return ChatService(
        intentService=IntentService(),
        ontologyService=object(),  # type: ignore[arg-type]
        datasourceService=object(),  # type: ignore[arg-type]
        modelRouterService=object(),  # type: ignore[arg-type]
        tokenUsageService=object(),  # type: ignore[arg-type]
        embeddingService=object(),  # type: ignore[arg-type]
        schemaIntrospectionService=object(),  # type: ignore[arg-type]
        graphTraversalService=object(),  # type: ignore[arg-type]
        agentRuntimeService=object(),  # type: ignore[arg-type]
        supplierNameResolver=object(),  # type: ignore[arg-type]
        kpiMatcher=object(),  # type: ignore[arg-type]
        llmFactory=factory,
    )


def _expectedConfidence(result: IntentResult) -> float:
    """契约口径的独立复算：与 _deriveSemanticState 同规则但不调它（防同错）。"""
    if result.intent in (IntentType.QUERY, IntentType.NEW_QUERY, IntentType.FOLLOW_UP):
        hits = sum(
            x is not None for x in (result.dimension, result.metric, result.chartType)
        )
        return min(1.0, hits * 0.4) if hits else RULE_CONFIDENCE_FALLBACK
    return RULE_CONFIDENCE_EXACT


class TestRoutingFreeze:
    """对拍：新入口与旧路径逐条 diff 为空 + 冻结期望矩阵（13 类全覆盖）。"""

    @pytest.fixture()
    def harness(self) -> _Harness:
        return _Harness()

    @pytest.mark.parametrize(
        ("question", "expectedNoState", "expectedWithState"),
        _ROUTING_FIXTURE,
        ids=[q for q, _, _ in _ROUTING_FIXTURE],
    )
    async def test_classify_and_recall_matches_legacy_path(
        self,
        harness: _Harness,
        question: str,
        expectedNoState: IntentType,
        expectedWithState: IntentType,
    ) -> None:
        legacy = IntentService()
        for hasPriorState, expected in (
            (False, expectedNoState),
            (True, expectedWithState),
        ):
            old = legacy.classifyResult(question, hasPriorState=hasPriorState)
            classified = await harness.classifyAndRecall(
                None, question, hasPriorState=hasPriorState, needRecall=False
            )
            new = classified.intentResult
            # 路由冻结：intent + 全部抽取字段逐条 diff 为空
            assert new == old, (
                f"路由漂移：{question!r} hasPriorState={hasPriorState}\n"
                f"old={old!r}\nnew={new!r}"
            )
            assert new.intent is expected, (
                f"期望矩阵冻结失败：{question!r} hasPriorState={hasPriorState} "
                f"期望 {expected} 实际 {new.intent}"
            )
            # needRecall=False：不召回、不加载状态
            assert classified.classes == []
            assert classified.recallInfo is None
            assert classified.state is None

    def test_fixture_covers_all_intent_types(self) -> None:
        covered = {
            intent for _, noState, withState in _ROUTING_FIXTURE
            for intent in (noState, withState)
        }
        assert covered == set(IntentType), (
            f"对拍集未覆盖全部意图：缺 {set(IntentType) - covered}"
        )


class TestSemanticStateContract:
    """semanticState 契约：出口恒填充 + 字段搬运 + 置信度口径 + 不可变。"""

    @pytest.mark.parametrize(
        ("question", "_no", "_with"),
        _ROUTING_FIXTURE,
        ids=[q for q, _, _ in _ROUTING_FIXTURE],
    )
    def test_semantic_state_always_populated(
        self, question: str, _no: IntentType, _with: IntentType
    ) -> None:
        service = IntentService()
        for hasPriorState in (False, True):
            result = service.classifyResult(question, hasPriorState=hasPriorState)
            state = result.semanticState
            assert state is not None, f"{question!r} semanticState 未填充"
            # v1 只搬运 classifyResult 已抽取的字段
            assert state.metric == result.metric
            assert state.dimension == result.dimension
            assert state.chartType == result.chartType
            # time/filters v1 恒 None（占位，B5 扩展）
            assert state.time is None
            assert state.filters is None
            # 置信度口径：确定性分支 1.0；查询家族按实体命中数；零证据 0.0
            assert state.ruleConfidence == _expectedConfidence(result), (
                f"{question!r} ruleConfidence={state.ruleConfidence} "
                f"≠ 契约复算 {_expectedConfidence(result)}"
            )

    def test_semantic_state_is_frozen(self) -> None:
        state = SemanticState(
            metric="m", dimension=None, chartType=None, ruleConfidence=0.4
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.metric = "x"  # type: ignore[misc]

    def test_explicit_semantic_state_overrides_derivation(self) -> None:
        """显式传入 semanticState 时不派生（通道 2 LLM 抽取结果的接入位）。"""
        explicit = SemanticState(
            metric="销售额", dimension="地区", chartType=ChartType.BAR,
            ruleConfidence=0.9,
        )
        result = IntentResult(intent=IntentType.QUERY, semanticState=explicit)
        assert result.semanticState is explicit


class TestClassifyAndRecallGuards:
    """入口行为守卫：召回跳过集合 / 边界校验 / 零 LLM 预算闸。"""

    async def test_non_recall_intents_skip_recall_even_when_requested(self) -> None:
        """不进 NL2SQL 的意图（如 CHITCHAT/SUPPLIER_360）needRecall=True 也跳过召回。"""
        harness = _Harness()
        for question in ("你好", "供应商 100001 的 360° 视图", "有哪些指标"):
            classified = await harness.classifyAndRecall(
                None, question, needRecall=True
            )
            assert classified.intentResult.intent not in _RECALL_INTENTS
            assert classified.classes == []
            assert classified.recallInfo is None

    async def test_recall_without_session_raises(self) -> None:
        """边界校验：QUERY 家族 + needRecall=True + session=None → 显式 ValueError。"""
        harness = _Harness()
        with pytest.raises(ValueError, match="session"):
            await harness.classifyAndRecall(
                None, "各供应商的收货数量汇总", needRecall=True
            )

    async def test_zero_llm_calls_across_fixture(self) -> None:
        """快通道预算闸：对拍集全量（66 条 × 两态）跑 classifyAndRecall，LLM 0 次。"""
        factory = _CountingLlmFactory()
        service = _buildCountingService(factory)
        for question, _, _ in _ROUTING_FIXTURE:
            for hasPriorState in (False, True):
                await service.classifyAndRecall(
                    None, question, hasPriorState=hasPriorState, needRecall=False
                )
        assert factory.callCount == 0
