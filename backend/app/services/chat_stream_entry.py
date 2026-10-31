"""流式入口编排（ChatService 的 StreamEntryMixin）。

`processMessageStream` / `_processMessageStreamInner` 组成 SSE 入口：meta
首帧 → 路由分支（chitchat / 领域命令 / clarify / 拦截卡片 / query / 多步）
→ token×N → done。承载 H4 断连兜底状态在入口侧挂上，以及 L1/L4 preamble
命中后的单帧 meta 协议。

模块级常量 `_OVERVIEW_TERMINAL_STATUS` 与函数 `_overviewStepPayload` 供
`_streamMultiStep`（chat_stream_multistep.py）消费：续跑时 `index <
startIndex` 的步不回放 pending，而是回放 DB 里已持久化的终态。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import IntentType
from app.domain.exceptions import (
    DomainError,
    LlmClientError,
    Nl2SqlError,
    ValidationError,
)
from app.domain.schemas import ChatRequest
from app.services.chat_constants import (
    META_INTENT_L1,
    META_INTENT_L4,
    ROUTING_LAYER_L1,
)
from app.services.chat_helpers import attachStreamPersistState
from app.services.error_classification import classifyError, sanitizeErrorMessage
from app.services.evidence_record_service import (
    resetChatSessionId,
    resetChatUserId,
    setChatSessionId,
    setChatUserId,
)
from app.services.intent_service import IntentResult
from app.services.messages_zh import MSG_INTERNAL_ERROR
from app.services.stream_events import (
    EVENT_CHART,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_META,
    EVENT_PLAN,
    EVENT_SQL,
    EVENT_TOKEN,
    ErrorType,
    StreamEvent,
)

logger = logging.getLogger(__name__)

# F7/IMP-6：概览回放的 DB 步状态 → 前端 `MultiStepStep.status` 终态映射。
# **只有**这两个会被回放成「已完成」：`prepareResume` 的前序步守卫只放行
# succeeded / compressed（multi_step_resume.py），故 `index < startIndex` 的步
# 必落其中之一。`failed` / `skipped` / `pending` / `running` 一律不回放。
_OVERVIEW_TERMINAL_STATUS: dict[str, str] = {
    "succeeded": "done",
    "compressed": "done",
}


def _overviewStepPayload(step, stepsByIdx: dict, startIndex: int) -> dict:
    """`multi_step_plan` 概览里单步的载荷。

    续跑时 `index < startIndex` 的步**不执行**（只在循环里 `continue`、不发任何事件），
    概览若一律下发 pending，这些**已成功**的步会在 UI 上从「已完成」退回「待执行」——
    「续跑省掉重跑」完全看不出来（DB 里它们确实是 succeeded）。故对它们回放**已持久化
    的终态**（唯一事实来源 = DB，不是前端记忆）。

    `>= startIndex` 的步（含汇总步）即将（重）跑，**不**带 status，由前端默认为
    pending —— 否则会把上一轮的失败当成本次终态显示。
    """
    payload: dict = {
        "stepIndex": step.index,
        "description": step.description,
        "subQuestion": step.sub_question,
        "aggregationOnly": step.aggregation_only,
    }
    if step.index < startIndex:
        persisted = stepsByIdx.get(step.index)
        replay = _OVERVIEW_TERMINAL_STATUS.get(getattr(persisted, "status", None))
        if replay is not None:
            payload["status"] = replay
    return payload


class StreamEntryMixin:
    """流式入口编排（由 ChatService 组合）。"""

    async def processMessageStream(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        *,
        user: CurrentUser | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式处理一条消息（入口包装：透传 chat session_id 到 evidence 记录）。

        v3.1 B2：流式链路内 execute_read_only 自动落 SQL_QUERY evidence，
        session_id 经 contextvar 透传（evidence_record_service）。生成器体内
        设置/复位：value 在查询调用栈里同步可见，断连/关闭时 finally 复位。
        R2：同时透传服务端 actor，chat 消息落库时打归属标（session_message.user_id）。
        """
        token = setChatSessionId(dto.sessionId)
        userToken = setChatUserId(user.userId if user is not None else None)
        try:
            async for event in self._processMessageStreamInner(dto, session, user=user):
                yield event
        finally:
            resetChatUserId(userToken)
            resetChatSessionId(token)

    async def _processMessageStreamInner(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        *,
        user: CurrentUser | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式处理一条消息。

        meta 事件最先产出（告知意图）；有上一轮状态时重新分类并二次产出 meta。
        chitchat 直接产出一条问候；DEFINE/MAP/METRIC 走 _streamDomainCommand（零 LLM）；
        CLARIFY 走 _streamClarify；拦截类意图（360°/风险/图推理/Agent 运行）走
        _streamInterceptCard（#207 审查 HIGH 修复：此前流式路径从不路由这些意图，
        默认 streaming UI 下卡片从未渲染）；其余走 _streamQuery。
        查询流水线中的领域异常转为 error 事件（4-1 携带 errorType），保证 SSE
        始终以结构化事件结束。4-4：闲聊/出错轮也持久化消息，历史链不断。

        user 可选（#207 安全修复）：API 层透传真实调用方，Agent 运行用它作 actor。

        H4：函数入口即武装断连兜底（`StreamPersistState.pending = True`）。客户端在
        任何一次 yield 之后断连都可能已经看到内容，而断连时生成器停在 `yield` 上时
        `finally` 不会触发 ⇒ 只有响应收尾的 background 钩子能补写（见
        `persistInterruptedStream`）。任何一次成功落库都会自动解除（不会重复写）。
        """
        persistState = attachStreamPersistState(session)
        persistState.pending = True
        persistState.startedAt = time.monotonic()
        # Phase 6.5：supplier name → code 预解析（同 processMessage；流式入口覆盖）
        try:
            dto = await self._prepareSupplierQuestion(session, dto)
        except ValidationError as exc:
            # 流式惯例（与下方 DomainError 分支同型）：结构化 error 事件
            await self._storeSessionMessages(
                session, dto.sessionId, dto.question, exc.message, None
            )
            yield StreamEvent(
                EVENT_ERROR,
                {
                    "error": sanitizeErrorMessage(exc, ErrorType.DOMAIN),
                    "errorType": ErrorType.DOMAIN.value,
                    "detail": exc.detail,
                },
            )
            return
        except Exception as exc:
            # 非预期失败（DB 连接中断等）：同样以结构化 error 事件结束 SSE，
            # 避免连接被强行中断（与 processMessageStream 文档保证一致）。
            logger.exception("supplier name 预解析失败: %s", exc)
            await self._storeSessionMessages(
                session, dto.sessionId, dto.question, MSG_INTERNAL_ERROR, None
            )
            yield StreamEvent(
                EVENT_ERROR,
                {
                    "error": sanitizeErrorMessage(exc, ErrorType.INTERNAL),
                    "errorType": ErrorType.INTERNAL.value,
                },
            )
            return

        # P0-1：流式入口共享 L1/L4 pre-amble（普通入口已接）
        # KPI 命中最优先短路（0 LLM cost）；L4 命中也短路；未命中继续分类流程。
        preamble = await self._runL1AndL4Preamble(dto, session, user)
        if preamble is not None:
            # F2c: merge into single meta frame (was 2-3 separate frames; each
            #不带 intent 的帧会抹掉前端刚设好的 intent）。
            meta_frame = {
                "intent": META_INTENT_L1 if preamble.routing_layer == ROUTING_LAYER_L1 else META_INTENT_L4,
                "routingLayer": preamble.routing_layer,
                "datasourceMismatch": preamble.datasource_mismatch,
            }
            if preamble.mismatch_warning is not None:
                meta_frame["warning"] = preamble.mismatch_warning
                meta_frame["code"] = "KPI_DATASOURCE_MISMATCH"
            if preamble.agent_run is not None:
                meta_frame["agentRun"] = preamble.agent_run
            yield StreamEvent(EVENT_META, meta_frame)
            # L4 带回的最终 SQL：不发这一帧，前端消息下就没有 SQL 卡片
            if preamble.sql:
                yield StreamEvent(EVENT_SQL, {"sql": preamble.sql})
            if preamble.chart_type is not None and preamble.chart_option is not None:
                yield StreamEvent(EVENT_CHART, {
                    "chartType": preamble.chart_type.value if hasattr(preamble.chart_type, "value") else str(preamble.chart_type),
                    "option": preamble.chart_option,
                    "rationale": preamble.visual_rationale,
                })
            yield StreamEvent(EVENT_TOKEN, {"content": preamble.answer})
            yield StreamEvent(EVENT_DONE, {
                "routingLayer": preamble.routing_layer,
                "tokenCostUsd": 0.0 if preamble.routing_layer == ROUTING_LAYER_L1 else None,
            })
            return

        # A7 通道 1 收口：分类+重分类统一走 classifyAndRecall 单入口。
        # 首帧 META 必须携带无状态分类结果（SSE 事件序列不变），故第一遍以
        # sessionId=None 纯分类；第二遍带 sessionId 完成「加载状态→重分类」。
        firstPass = await self.classifyAndRecall(session, dto.question, needRecall=False)
        result = firstPass.intentResult
        yield StreamEvent(EVENT_META, {"intent": result.intent.value})

        if result.intent == IntentType.CHITCHAT:
            async for event in self._streamChitchat(dto, session):
                yield event
            return

        try:
            classified = await self.classifyAndRecall(
                session, dto.question, sessionId=dto.sessionId, needRecall=False,
                # F1：scope 取 dbUserId（int 主键）而非 userId（X-User-Id 字符串）——
                # SessionQueryState.user_id 是 BigInteger，插字符串会直接 500
                # （asyncpg: operator does not exist: bigint = character varying）。
                # stub/anonymous 头解析不到 DB 用户时 dbUserId 为 None，退化为不过滤。
                userId=user.dbUserId if user is not None else None,
                datasourceId=dto.datasourceId,
            )
            state = classified.state
            if state is not None:
                result = classified.intentResult
                yield StreamEvent(EVENT_META, {"intent": result.intent.value})
                if result.intent == IntentType.CHITCHAT:
                    # 重分类后仍可能收敛为闲聊（如问候中含指代词）：同样短路
                    async for event in self._streamChitchat(dto, session):
                        yield event
                    return
            if result.intent in (IntentType.DEFINE, IntentType.MAP, IntentType.METRIC):
                async for event in self._streamDomainCommand(dto, session, result, user=user):
                    yield event
                return
            if result.intent == IntentType.CLARIFY:
                async for event in self._streamClarify(dto, session):
                    yield event
                return
            # #207 审查 HIGH 修复：拦截类意图在流式路径同样路由（token + done 卡片对象），
            # 覆盖 SUPPLIER_360 / SUPPLIER_RISK / GRAPH_REASONING / AGENT_RUN 四个卡片意图。
            if result.intent in (
                IntentType.SUPPLIER_360,
                IntentType.SUPPLIER_RISK,
                IntentType.GRAPH_REASONING,
                IntentType.AGENT_RUN,
            ):
                async for event in self._streamInterceptCard(dto, session, result, user):
                    yield event
                return
            _stream_t0 = time.monotonic()
            async for event in self._streamQuery(
                dto, session, result.intent, state, result.chartType,
                suggestion=result.suggested_agent, _t0=_stream_t0,
                semanticState=result.semanticState,  # B5 HIGH-1
                user=user,
            ):
                yield event
        except LlmClientError as exc:
            errorType = classifyError(exc)
            logger.warning("流式查询 LLM 失败: %s", exc.message)
            # P2-1 review M1：原始 provider 文案进日志，SSE payload 用脱敏文案
            # exc.message 落库供排障（用户下次看历史对话能定位问题），不暴露给前端
            await self._storeSessionMessages(session, dto.sessionId, dto.question, exc.message, None)
            yield StreamEvent(
                EVENT_ERROR,
                {
                    "error": sanitizeErrorMessage(exc, errorType),
                    "errorType": errorType.value,
                },
            )
        except DomainError as exc:
            errorType = classifyError(exc)
            logger.warning("流式查询失败: %s", exc.message)
            await self._storeSessionMessages(session, dto.sessionId, dto.question, exc.message, None)
            if isinstance(exc, Nl2SqlError) and exc.lastPlan is not None:
                yield StreamEvent(EVENT_PLAN, {"plan": exc.lastPlan.to_dict()})
            yield StreamEvent(
                EVENT_ERROR,
                {
                    "error": sanitizeErrorMessage(exc, errorType),
                    "errorType": errorType.value,
                    "detail": exc.detail,
                },
            )
        except Exception as exc:
            # 未预期异常（DB 连接中断 / 数据解析错误等）：记录完整上下文并产出结构化
            # 错误事件，保证 SSE 连接始终以 error 事件结束而不是被强行中断。
            logger.exception("流式查询未预期错误: %s", exc)
            await self._storeSessionMessages(session, dto.sessionId, dto.question, MSG_INTERNAL_ERROR, None)
            yield StreamEvent(
                EVENT_ERROR,
                {
                    "error": sanitizeErrorMessage(exc, ErrorType.INTERNAL),
                    "errorType": ErrorType.INTERNAL.value,
                },
            )


__all__ = [
    "StreamEntryMixin",
    "_OVERVIEW_TERMINAL_STATUS",
    "_overviewStepPayload",
]
