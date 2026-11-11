"""流式回答输出：块间超时保护 + 主模型失败降级（SSE 的 answer 阶段）。

ChatService 以 mixin 方式组合本模块（class ChatService(ChatStreamOutputMixin)），
提供：
- `_iterStreamChunks`：逐块读取流式响应，块间超时保护，防止 LLM 挂起占用连接。
- `_streamAnswerWithFallback`：主模型失败且未产出 token 时降级到最便宜可用模型。
- `_runL1AndL4Preamble`：L1 KPI + L4 Agent Loop 预检，普通与流式入口共用。

mixin 契约（依赖 ChatService 提供的实例成员）：
- `self._llmFactory(config)`：按配置创建 LLM 客户端。
- `self._modelRouter.selectFallbackModel(configs, excludeId)`：选择降级模型。
- `self._recordUsage(...)`：记录 Token 用量审计行。
- `self._buildAnswerPrompt(question, sql, data)`：构建回答 user prompt。
- `self._kpiMatcher`：KpiSemanticMatchService 实例。
- `self._buildL1Response(match, session)`：L1 命中时构建回答。
- `self._resolveKpiCatalog(code, session)`：查 KpiCatalog。
- `self._handleNl2SqlAgent(session, dto, user)`：L4 Agent Loop。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import ChartType, IntentType
from app.domain.error_messages import (
    MSG_INTERNAL_ERROR_GENERIC,
    MSG_KPI_DATASOURCE_MISMATCH,
)
from app.domain.exceptions import LlmClientError
from app.domain.models import LlmConfig, OntologyMetric
from app.domain.schemas import ChatRequest
from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage, StreamChunk
from app.services.chart_thresholds import loadFullDataThreshold
from app.services.chat_constants import (
    ROUTING_LAYER_KPI_CLARIFY,
    ROUTING_LAYER_KPI_TIER_B,
    ROUTING_LAYER_L1,
    USAGE_PURPOSE_ANSWER_STREAM_FAILED,
    USAGE_PURPOSE_FALLBACK_ANSWER,
)
from app.services.kpi_execution_service import (
    REASON_EXECUTION_ERROR,
    REASON_EXECUTION_SOURCE_UNKNOWN,
    TIER_A,
    TIER_B,
    TIER_C,
    KpiExecutionPlan,
    hasExplicitTimeScope,
    planKpiExecution,
)
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_reconcile import reconcileMetric
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf
from app.services.kpi_metric_switches import (
    isKpiMatchContainmentEnabled,
    isKpiMetricTierAEnabled,
)
from app.services.kpi_semantic_match_service import (
    KpiMatchResult,
    KpiMatchRoute,
    buildKpiClarifyAnswer,
)
from app.services.prompts_zh import PROMPT_ANSWER_SYSTEM as _ANSWER_SYSTEM_PROMPT
from app.services.visual_rationale import buildVisualRationale

logger = logging.getLogger(__name__)

_STREAM_CHUNK_TIMEOUT_SECONDS = 45.0  # 回答流块间超时：防止 LLM 挂起占用连接/会话（4-3：120s→45s）


class ChatStreamOutputMixin:
    """流式回答输出的公共能力（mixin，被 ChatService 组合）。"""

    async def _iterStreamChunks(
        self, client: BaseLlmClient, messages: list[LlmMessage], model: str
    ) -> AsyncIterator[StreamChunk]:
        """逐块读取流式响应，块间超时保护。

        LLM 挂起（长时间无新块）时按超时中止并转为 LlmClientError，
        保证 SSE 不会无限期占用连接与数据库会话。
        """
        generator = client.completeStream(messages=messages, model=model)
        while True:
            try:
                chunk = await asyncio.wait_for(
                    generator.__anext__(), timeout=_STREAM_CHUNK_TIMEOUT_SECONDS,
                )
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise LlmClientError(
                    f"回答流超过 {_STREAM_CHUNK_TIMEOUT_SECONDS}s 无新块，触发超时中止",
                    provider=model,
                ) from exc
            yield chunk

    async def _streamAnswerWithFallback(
        self,
        session: AsyncSession,
        sessionId: str,
        configs: list[LlmConfig],
        primary: LlmConfig,
        dto: ChatRequest,
        sql: str,
        data: list[dict],
        forced: bool = False,
        history: str = "",
    ) -> AsyncIterator[tuple[StreamChunk, LlmConfig, tuple[int, int]]]:
        """流式回答：主模型失败且未产出任何 token 时降级到最便宜可用模型。

        history 为最近对话历史（1-5，可为空串），随回答 prompt 一并注入，
        支持跨轮连贯与对比。与降级语义无关，仅在构造 messages 时使用。

        降级在首次失败时才惰性求值（与 _callWithFallback 语义一致，避免无谓查询）。
        产出 (chunk, 实际服务模型, (0,0)) 三元组；第三元恒为 (0,0)——流式失败时
        客户端不会回传已消耗 token（done 块未到达），浪费 token 不可计量，仅以零
        token 审计行作为失败标记：
        - 主模型失败且未产出 token → 降级，记录 purpose=USAGE_PURPOSE_FALLBACK_ANSWER
        - 已产出 token 后中断（无法回退）→ 记录 purpose=USAGE_PURPOSE_ANSWER_STREAM_FAILED 后上抛
        逐块读取经 _iterStreamChunks 块间超时保护，防止 LLM 挂起占用连接。
        """
        # Task 2：FULL_DATA_THRESHOLD 迁 system_config 后，持 session 的调用方现读。
        full_data_threshold = await loadFullDataThreshold(session)
        messages = [
            LlmMessage(role="system", content=_ANSWER_SYSTEM_PROMPT),
            LlmMessage(
                role="user",
                content=self._buildAnswerPrompt(
                    dto.question, sql, data, history=history,
                    full_data_threshold=full_data_threshold,
                ),
            ),
        ]
        emittedContent = False
        attempt = primary
        while True:
            try:
                async for chunk in self._iterStreamChunks(
                    self._llmFactory(attempt), messages, attempt.model_name,
                ):
                    if chunk.content:
                        emittedContent = True
                    yield chunk, attempt, (0, 0)
                return
            except LlmClientError as exc:
                if emittedContent:
                    # 已产出 token，客户端已收到部分内容，无法回退；记失败标记审计行
                    await self._recordUsage(
                        session, sessionId, attempt, 0, 0, purpose=USAGE_PURPOSE_ANSWER_STREAM_FAILED,
                    )
                    logger.warning("回答流中断（已产出 token，不再降级）: %s", exc.message)
                    raise
                if attempt is not primary or forced:
                    raise
                fallback = self._modelRouter.selectFallbackModel(configs, primary.id)
                if fallback is None:
                    raise
                logger.warning("回答流模型 %s 失败，尝试降级: %s", attempt.model_name, exc.message)
                await self._recordUsage(
                    session, sessionId, attempt, 0, 0, purpose=USAGE_PURPOSE_FALLBACK_ANSWER,
                )
                attempt = fallback


# ===========================================================================
# L1 KPI + L4 Agent pre-amble（普通与流式共用）
# ===========================================================================


class PreambleResult(BaseModel):
    """L1/L4 命中后的封装；含生成 SSE 帧所需全部字段。"""

    # "L1C" = KPI 多命中歧义澄清（见 ROUTING_LAYER_KPI_CLARIFY）。
    # "L1B" = Tier B 降级哨兵（见 ROUTING_LAYER_KPI_TIER_B）：本 preamble **不终结**，
    # 入口拆包后继续主 NL2SQL 流水线。Literal 不能用常量，故写字面量；str 值由
    # chat_constants 单点定义，测试锁定两者一致。
    routing_layer: Literal["L1", "L4", "L1C", "L1B"]
    answer: str
    chart_type: Any = None  # ChartType | None — avoid import cycle
    chart_option: dict | None = None
    visual_rationale: dict | str | None = None
    # L1 KPI 命中信息（用于 ChatResponse kpi_code / kpi_name / confidence 字段）
    kpi_code: str | None = None
    kpi_name: str | None = None
    confidence: float | None = None  # KpiMatchResult.confidence
    kpi_match_method: str | None = None  # KpiMatchResult.method（L1/L1B wire 诊断）
    # Tier 分流观测字段（D5 起由 ChatResponse / 流式 meta 消费；D4 只负责携带）
    execution_tier: str | None = None
    degraded_reason: str | None = None
    # L4
    agent_run: dict | None = None
    # L4 最终 SQL（流式入口据此下发 sql 帧；非流式入口走 full_response.sql）
    sql: str | None = None
    # L4 执行轨迹（2026-10-31）：流式入口据此在 token 前下发 agent_trace 帧；
    # 非流式入口走 full_response.agentTrace。
    agent_trace: dict | None = None
    # L1 cross-datasource mismatch
    datasource_mismatch: bool = False
    mismatch_warning: str | None = None
    # 完整 ChatResponse（非流式入口需要完整响应；流式入口只用上面那些字段）。
    # 用 Any 破循环依赖（同 chart_type 的既有做法）：本模块在 ChatResponse 之前导入，
    # 若写真实类型注解，pydantic 在建类时会求值该注解（即使有 from __future__ annotations
    # 也只在 type annotation 层面延迟，BaseModel.model_fields 的求值仍需运行时解析），
    # 导致 NameError / PydanticUndefinedAnnotation；用 TYPE_CHECKING + 字符串字面量
    # 也同样会在 pydantic 求值时失败（pydantic 不走 __annotations__ 的 TYPE_CHECKING 分支）。
    # 唯一可行方案是 TypeVar + model_rebuild()，代价远大于收益；保持 Any 并记录原因。
    full_response: Any = None


def requireFullResponse(preamble: PreambleResult, routing_layer: str) -> Any:
    """守卫：full_response 未填充则快速失败。

    这是内部不变量检查（代码 bug，不是用户输入），所以：
    - 用 RuntimeError（不是 DomainError）→ 落到 500 兜底 handler，
      不走 DomainError 那条把 message 原样写进响应体的路径；
    - 文案取 error_messages 常量，不在 services 层硬编码，且不含任何内部标识；
    - 详细诊断（routing_layer、preamble 各字段）只进日志。
    """
    if preamble.full_response is not None:
        return preamble.full_response
    logger.error(
        "PreambleResult invariant broken: full_response is None for routing_layer=%s "
        "(preamble fields: routing_layer=%r, answer=%r, chart_type=%r). "
        "This indicates a code bug: a PreambleResult construction point failed to "
        "populate full_response. Check L1L4PreambleMixin._runL1AndL4Preamble.",
        routing_layer,
        preamble.routing_layer,
        preamble.answer,
        preamble.chart_type,
    )
    raise RuntimeError(MSG_INTERNAL_ERROR_GENERIC)


def splitTierBPreamble(
    preamble: PreambleResult | None,
) -> tuple[PreambleResult | None, PreambleResult | None]:
    """Tier B 哨兵拆包：返回 (终结型 preamble, Tier B 提示)。

    Tier B 的 preamble 只是「这次别走 L4」的提示，**不终结**请求：入口拿到
    ``(None, hint)`` 后继续主 NL2SQL 流水线（与 ``routing_layer == "L1B"`` 对应）。
    其余层原样返回 ``(preamble, None)``。
    """
    if (
        preamble is not None
        and preamble.routing_layer == ROUTING_LAYER_KPI_TIER_B
    ):
        return None, preamble
    return preamble, None


def kpiHintFields(hint: PreambleResult | None) -> dict[str, Any]:
    """Tier B 提示 → SSE meta 帧字段（前端 camelCase wire 契约）。

    datasource mismatch 与终结型 L1 meta 帧保持同一组键（``datasourceMismatch``
    / ``warning`` / ``code``），否则重分类后的第二帧会把警告冲掉。
    """
    if hint is None:
        return {}
    fields: dict[str, Any] = {
        "kpiCode": hint.kpi_code,
        "kpiName": hint.kpi_name,
        "confidence": hint.confidence,
        "matchMethod": hint.kpi_match_method,
        "executionTier": hint.execution_tier,
        "degradedReason": hint.degraded_reason,
    }
    if hint.datasource_mismatch:
        fields["datasourceMismatch"] = True
        fields["warning"] = hint.mismatch_warning
        fields["code"] = "KPI_DATASOURCE_MISMATCH"
    return fields


# = 会落到 `chat_service._handleGenericQuery` 的意图集合：这些意图在
# `processMessage` 中会经过 `_attachKpiHint`，因此两入口都必须携带 Tier B 观测字段。
# 早退集合是 CHITCHAT / DEFINE / MAP / METRIC / SUPPLIER_360 / SUPPLIER_RISK /
# GRAPH_REASONING / AGENT_RUN —— 新增意图时必须先确认它归哪一边，否则会复现
# 「流式有、非流式无」或反向的观测不一致。
_TIER_HINT_INTENTS: frozenset[IntentType] = frozenset({
    IntentType.QUERY,
    IntentType.NEW_QUERY,
    IntentType.REFINE,
    IntentType.FOLLOW_UP,
    IntentType.CLARIFY,
})


def tierHintClearedFields(hint: PreambleResult) -> dict[str, None]:
    """Tier B 字段的显式清空帧（第二帧 META 用）。

    重分类后意图若落到早退集合（CHITCHAT / DEFINE / MAP / METRIC ...），非流式
    入口不会挂 hint，``executionTier`` 为 ``null``；流式入口首帧也可能已经发过
    Tier B 字段，而前端逐帧合并 —— 只省略键不会清掉旧值。故第二帧显式发送一组
    ``None``，让两入口的最终观测一致（``executionTier: null``）。

    键集合**由 :func:`kpiHintFields` 派生**：包含 datasource mismatch 时追加的
    ``datasourceMismatch`` / ``warning`` / ``code``，将来新增字段也自动覆盖。
    """
    return {key: None for key in kpiHintFields(hint)}


def tierHintFields(hint: PreambleResult | None, intent: IntentType) -> dict[str, Any]:
    """仅对最终会落到通用查询主链路的意图发 Tier B 字段。

    ``_TIER_HINT_INTENTS`` = 会落到 ``chat_service._handleGenericQuery`` 的意图集合
    （QUERY / NEW_QUERY / REFINE / FOLLOW_UP / CLARIFY）。早退集合是 CHITCHAT /
    DEFINE / MAP / METRIC / SUPPLIER_360 / SUPPLIER_RISK / GRAPH_REASONING /
    AGENT_RUN —— 新增意图时必须先确认它归哪一边。流式入口的 META 帧在意图分支
    之前产出，必须用**本帧解析出的意图**复刻同一门控，否则会出现「流式宣称
    executionTier=B、非流式却是 null」（或反向）的观测性谎言。
    重分类可能改变意图，故调用方逐帧传入各自的 ``intent``，不得复用首帧结果。
    """
    if intent not in _TIER_HINT_INTENTS:
        return {}
    return kpiHintFields(hint)


def secondFrameTierFields(
    hint: PreambleResult | None, intent: IntentType
) -> dict[str, Any]:
    """第二帧 META 的 Tier 字段：门控不命中时显式清空首帧留下的字段。

    首帧可能已发过 Tier B 观测字段；重分类后意图若落到早退集合，非流式入口
    的对应字段为 ``null``。前端逐帧合并，省略键清不掉旧值，故 hint 非 None 时
    发送全 ``None`` 的同键集合。``hint is None`` 时无字段可清，保持空字典。
    """
    fields = tierHintFields(hint, intent)
    if hint is not None and fields == {}:
        return tierHintClearedFields(hint)
    return fields


class L1L4PreambleMixin:
    """抽 KPI/L4 预检为共享 mixin，让普通 + 流式入口共用同一条路径。"""

    # Subclasses must provide _kpiMatcher (KpiSemanticMatchService)
    _kpiMatcher: Any

    async def _containmentEnabled(self, session: AsyncSession) -> bool:
        """v2 灰度开关判定（单点，测试可覆写以避免 DB 依赖）。"""
        return await isKpiMatchContainmentEnabled(session)

    async def _ensureKpiCacheFresh(self, session: AsyncSession) -> None:
        """v2 匹配前确保缓存新鲜（单点，测试可覆写以避免 DB 依赖）。

        savepoint：``ensureFresh`` 可能写库（TTL 重建），失败不能把 chat 主流程的
        事务打成 failed 态（后续语句全 PendingRollbackError）。
        """
        async with session.begin_nested():
            await get_kpi_match_cache().ensureFresh(session)

    async def _routeKpi(
        self, question: str, session: AsyncSession
    ) -> KpiMatchRoute:
        """KPI 匹配路由：v2 走 matchRoute（先 ensureFresh），v1 走旧 match()。"""
        if not await self._containmentEnabled(session):
            match = await self._kpiMatcher.match(question)
            if match is None:
                return KpiMatchRoute(kind="none")
            return KpiMatchRoute(kind="hit", matches=(match,))
        await self._ensureKpiCacheFresh(session)
        return await self._kpiMatcher.matchRoute(question)

    def _buildKpiClarifyPreamble(
        self, question: str, matches: tuple[KpiMatchResult, ...]
    ) -> PreambleResult:
        """多命中 ⇒ 澄清候选（0 LLM、无图表；前端按 intent=clarify 渲染正文）。

        ChatResponse 在这里**延迟导入**：本模块在 ChatResponse 之前被导入，
        模块级 import 会形成循环依赖（与 full_response 的 Any 注解同源问题）。
        """
        from app.domain.enums import IntentType
        from app.domain.schemas import ChatResponse

        answer = buildKpiClarifyAnswer(question, matches)
        response = ChatResponse(
            answer=answer,
            intent=IntentType.CLARIFY.value,
            data=[{"code": m.code, "name": m.kpi_name or m.code} for m in matches],
            tokensUsed=0,
            cost=0.0,
        )
        return PreambleResult(
            routing_layer=ROUTING_LAYER_KPI_CLARIFY,
            answer=answer,
            full_response=response,
        )

    async def _tierAEnabled(self, session: AsyncSession | None) -> bool:
        """Tier A 执行器开关（单点，测试可覆写；session=None 纯单元 ⇒ 关）。"""
        return await isKpiMetricTierAEnabled(session)

    async def _planKpiTier(
        self, question: str, kpi: Any | None, session: AsyncSession | None
    ) -> KpiExecutionPlan:
        """运行时 Tier 判定：与诊断接口共用 reconcileMetric，避免两套口径。"""
        switchOn = await self._tierAEnabled(session)
        hasExplicitTime = hasExplicitTimeScope(question)

        # 开关关：不碰 kpi 属性（单测里的 kpi 桩可能只有 code/name/keywords）。
        if not switchOn:
            return planKpiExecution(
                switch_on=False, kpi_found=kpi is not None, metric_present=False,
                execution_source=None, tier_a_eligible=False, has_datasource=False,
                formula_present=False, has_explicit_time_scope=hasExplicitTime,
            )
        if kpi is None:
            return planKpiExecution(
                switch_on=True, kpi_found=False, metric_present=False,
                execution_source=None, tier_a_eligible=False, has_datasource=False,
                formula_present=False, has_explicit_time_scope=hasExplicitTime,
            )

        metric = (
            await session.get(OntologyMetric, kpi.metric_id)
            if kpi.metric_id is not None else None
        )
        # metric 缺失时不做对账（techniqueOf(None) 虽可构造，但此时对账结论无意义）。
        reconcile = (
            reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))
            if metric is not None else None
        )
        plan = planKpiExecution(
            switch_on=True,
            kpi_found=True,
            metric_present=metric is not None,
            execution_source=(metric.execution_source if metric is not None else None),
            tier_a_eligible=bool(reconcile is not None and reconcile.isTierAEligible),
            has_datasource=kpi.datasource_id is not None,
            formula_present=bool(metric is not None and (metric.formula or "").strip()),
            has_explicit_time_scope=hasExplicitTime,
        )
        if plan.reason == REASON_EXECUTION_SOURCE_UNKNOWN:
            # 运营/治理面可见：未知执行源意味着该 KPI 永远走不进 Tier A，
            # 需要人修 execution_source。%r 转义，防控制字符伪造日志行。
            logger.warning(
                "未知 execution_source kpi=%s source=%r datasource_id=%s",
                kpi.kpi_code,
                metric.execution_source if metric is not None else None,
                kpi.datasource_id,
            )
        return plan

    async def _executeTierA(
        self, match: KpiMatchResult, kpi: Any | None, session: AsyncSession | None
    ) -> tuple[Any | None, str | None]:
        """Tier A 执行钩子默认值（真实实现在 ChatService，下一个任务落地）。

        D4 先以「失败降级」占位：即使 Tier A 开关被误开且路由走到了 Tier A，
        这里也只返回 ``(None, REASON_EXECUTION_ERROR)`` ⇒ Tier B 哨兵，绝不 500。
        """
        return None, REASON_EXECUTION_ERROR

    @staticmethod
    def _tierBPreamble(
        match: KpiMatchResult,
        kpi: Any | None,
        reason: str | None,
        userDatasourceId: int | None = None,
    ) -> PreambleResult:
        """Tier B 哨兵：preamble 不终结，入口拆包后继续主 NL2SQL 流水线。

        datasource mismatch 必须随哨兵一路带到 SSE meta（与终结型 L1 同口径），
        否则 Tier B 走主链路时那条「KPI 绑定数据源 ≠ 用户所选数据源」的警告
        会被静默吞掉（D4 评审 Important）。
        """
        mismatch = (
            kpi is not None
            and kpi.datasource_id is not None
            and kpi.datasource_id != userDatasourceId
        )
        warning = (
            MSG_KPI_DATASOURCE_MISMATCH.format(
                datasource_id=kpi.datasource_id,
                user_datasource_id=userDatasourceId,
            )
            if mismatch else None
        )
        return PreambleResult(
            routing_layer=ROUTING_LAYER_KPI_TIER_B,
            answer="",
            kpi_code=match.code,
            kpi_name=(kpi.kpi_name if kpi is not None else match.code),
            confidence=match.confidence,
            kpi_match_method=match.method,
            execution_tier=TIER_B,
            degraded_reason=reason,
            datasource_mismatch=mismatch,
            mismatch_warning=warning,
        )

    def _buildTierCResponse(
        self, match: KpiMatchResult, kpi: Any, reason: str | None
    ) -> Any:
        """Tier C：指标不可确定性计算 ⇒ 只给口径说明（0 LLM）。

        reason 透传计划原因（execution_source_none / metric_missing /
        datasource_missing / execution_source_unknown），不再硬编码单一常量。

        ChatResponse 在函数内延迟导入（同 ``_buildKpiClarifyPreamble`` 的既有模式，
        避免本模块与 schemas 的循环依赖）。
        """
        from app.domain.schemas import ChatResponse

        name = kpi.kpi_name or match.code
        answer = (
            f"指标「{name}」：{kpi.business_definition or '暂无口径说明'}"
            "（暂不可确定性计算）"  # 设计 §6.1 原文措辞，集成测试按此断言
        )
        rationale = buildVisualRationale(
            ruleId="R00_EMPTY_TABLE", kind=ChartType.KPI, rowCount=0,
        )
        return ChatResponse(
            answer=answer,
            intent="l1_match",
            data=None,
            chartType=None,
            chartOption=None,
            visual_rationale=rationale.to_dict(),
            kpi_code=match.code,
            kpi_name=name,
            confidence=match.confidence,
            match_method=match.method,
            tokensUsed=0,
            cost=0.0,
            execution_tier=TIER_C,
            degraded_reason=reason,
        )

    async def _runL1AndL4Preamble(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        user: CurrentUser | None,
    ) -> PreambleResult | None:
        """KPI 优先（命中短路）；未命中则尝试 L4；仍未命中返 None（让主路径继续）。

        行为对 source-provider 改：
        - KPI 用 dto.datasourceId 比较 feat.datasource_id（真 FK 列，非 data_source text）；
          不匹配给 mismatch_warning（不静默）
        - L4 不改行为
        """
        # 1. KPI 路由：v2（包含式）开启走 matchRoute；否则回落 v1 关键字匹配。
        #    任何故障（含缓存预热失败）都降级到「无匹配」，不阻断 LLM 流水线。
        route: KpiMatchRoute | None = None
        try:
            route = await self._routeKpi(dto.question, session)
        except Exception:  # noqa: BLE001 — 降级到原 LLM 流水线
            logger.warning("L1 KPI match failed, falling back to LLM", exc_info=True)
            route = None

        if route is not None and route.kind == "ambiguous":
            # 等长跨 KPI 并列：不猜、不执行 SQL，直接返回澄清候选（0 LLM）。
            return self._buildKpiClarifyPreamble(dto.question, route.matches)

        match = route.matches[0] if route is not None and route.kind == "hit" else None
        if match is not None:
            try:
                # 先取 KPI 行：四条分支都要用它（Tier 判定 / 文案 / datasource 比较）。
                kpi = await self._resolveKpiCatalog(match.code, session)
                plan = await self._planKpiTier(dto.question, kpi, session)
            except Exception:  # noqa: BLE001 — 降级到原 L4 流水线，绝不上抛请求路径
                # 与原 _buildL1Response 的容错语义一致：目录/开关/对账任何故障都只记
                # warning 并落到下面的 L4 块（非流式不 500、流式不中断）。
                logger.warning(
                    "KPI tier routing failed kpi=%s, falling back to L4",
                    match.code, exc_info=True,
                )
            else:
                if plan.tier == TIER_B:
                    # L4 不注入契约块：Tier B 必须交给主 NL2SQL 流水线。
                    return self._tierBPreamble(
                        match, kpi, plan.reason, dto.datasourceId
                    )

                if plan.tier == TIER_A:
                    try:
                        l1, degradedReason = await self._executeTierA(
                            match, kpi, session
                        )
                    except Exception:  # noqa: BLE001 — Tier A 失败降级到 Tier B
                        logger.warning(
                            "Tier A execution failed kpi=%s, degrading to Tier B",
                            match.code, exc_info=True,
                        )
                        l1, degradedReason = None, REASON_EXECUTION_ERROR
                    if l1 is None:
                        return self._tierBPreamble(
                            match, kpi, degradedReason or REASON_EXECUTION_ERROR,
                            dto.datasourceId,
                        )
                elif plan.tier == TIER_C:
                    l1 = self._buildTierCResponse(match, kpi, plan.reason)
                else:  # TIER_LEGACY：开关关 / 目录缺失 ⇒ 现有 FD 执行路径
                    l1 = await self._buildL1Response(match, session)

                if l1 is not None:
                    # P0 合并项 6：仅在 KPI 实际绑定了数据源时比较（沿用原判断）。
                    mismatch = (
                        kpi is not None
                        and kpi.datasource_id is not None
                        and kpi.datasource_id != dto.datasourceId
                    )
                    warning = (
                        MSG_KPI_DATASOURCE_MISMATCH.format(
                            datasource_id=kpi.datasource_id,
                            user_datasource_id=dto.datasourceId,
                        )
                        if mismatch else None
                    )
                    return PreambleResult(
                        routing_layer=ROUTING_LAYER_L1,
                        answer=l1.answer,
                        chart_type=l1.chartType,
                        chart_option=l1.chartOption,
                        visual_rationale=l1.visual_rationale,
                        kpi_code=match.code,
                        kpi_name=l1.kpi_name,
                        confidence=match.confidence,
                        kpi_match_method=match.method,
                        execution_tier=plan.tier,
                        degraded_reason=plan.reason,
                        datasource_mismatch=mismatch,
                        mismatch_warning=warning,
                        full_response=l1,
                    )

        # 2. L4 Agent Loop
        l4 = await self._handleNl2SqlAgent(session, dto, user=user)
        if l4 is not None:
            return PreambleResult(
                routing_layer="L4",
                answer=l4.answer,
                agent_run=(
                    l4.agent_run.model_dump(mode="json", by_alias=True)
                    if l4.agent_run is not None else None
                ),
                sql=l4.sql,
                agent_trace=l4.agent_trace,
                full_response=l4,
            )

        return None


__all__ = [
    "ChatStreamOutputMixin",
    "L1L4PreambleMixin",
    "PreambleResult",
    "kpiHintFields",
    "secondFrameTierFields",
    "tierHintClearedFields",
    "tierHintFields",
    "splitTierBPreamble",
    "_STREAM_CHUNK_TIMEOUT_SECONDS",
    "_ANSWER_SYSTEM_PROMPT",
]
