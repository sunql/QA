"""领域命令 / Agent / 供应商 / 图推理（ChatService 的 DomainCommandMixin）。

CLARIFY 概念解释、DEFINE/MAP/METRIC 领域命令（本体 CRUD，零 LLM）、供应商 360°/风险、
Agent 运行时、知识图谱多跳推理 的拦截式 handler，以及它们的 SSE 形式。均跳过 NL2SQL
流水线；失败语义统一转友好 answer（不向用户抛领域异常，避免「不存在 vs 无权限」侧信道）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import IntentType
from app.domain.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.domain.error_messages import (
    MSG_AGENT_NOT_FOUND_BY_CODE,
    MSG_AGENT_RUN_FAILED,
    MSG_AGENT_RUN_MISSING_CODE,
    MSG_GRAPH_TRAVERSAL_NOT_FOUND,
    MSG_SCHEMA_CHAT_SUPPLIER_KEY_MISSING,
)
from app.domain.schemas import (
    ChatRequest,
    ChatResponse,
    ExtractedEntities,
    OntologyClassCreate,
    OntologyMetricCreate,
    OntologyPropertyUpdate,
)
from app.infrastructure.llm.base_client import LlmMessage
from app.services.chat_constants import (
    AGENT_RUN_ERROR_AGENT_NOT_FOUND,
    AGENT_RUN_ERROR_AGENT_NOT_RUNNABLE,
    AGENT_RUN_ERROR_BAD_INPUT,
    AGENT_RUN_ERROR_PERMISSION_DENIED,
    AGENT_RUN_ERROR_UNEXPECTED,
    AUDIT_ACTION_CREATE,
    AUDIT_ENTITY_AGENT_RUN_LOG,
    AUDIT_RUN_STATUS_FAILED,
    AUDIT_RUN_STATUS_SUCCESS,
    USAGE_PURPOSE_AGENT_RUN,
    USAGE_PURPOSE_CLARIFY,
    USAGE_PURPOSE_SUPPLIER_RISK,
)
from app.services.chat_helpers import _PipelineContext, _audit
from app.services.graph_traversal_service import resolveChatMaxHops
from app.services.intent_service import IntentResult
from app.services.messages_zh import (
    MSG_CLASS_ALIAS_SUFFIX,
    MSG_CLASS_CREATED,
    MSG_DEFINE_CLASS_GUIDE_EXAMPLE,
    MSG_DEFINE_CLASS_GUIDE_PREFIX,
    MSG_DEFINE_METRIC_GUIDE_EXAMPLE,
    MSG_DEFINE_METRIC_GUIDE_PREFIX,
    MSG_GRAPH_TRAVERSAL_UNAVAILABLE,
    MSG_MAP_PROPERTY_GUIDE,
    MSG_MAP_PROPERTY_NOT_FOUND,
    MSG_MAP_PROPERTY_OK,
    MSG_MAP_PROPERTY_RETRY_HINT,
    MSG_METRIC_DEFINED,
    MSG_METRIC_LIST_HEADER,
    MSG_NO_METRICS_DEFINED,
    MSG_SUPPLIER_360_NOT_FOUND,
    MSG_SUPPLIER_RISK_NOT_FOUND,
)
from app.services.stream_events import EVENT_DONE, EVENT_TOKEN, StreamEvent
from app.services.supplier_360_service import Supplier360Service
from app.services.supplier_risk_service import SupplierRiskService, buildRiskAnswer

logger = logging.getLogger(__name__)

_CLARIFY_SYSTEM_PROMPT = (
    "你是一名企业数据分析助手。用户正在询问某个业务概念/术语的含义，"
    "请结合提供的本体元数据用简洁的中文解释，不要编造、不要输出 SQL。"
)


class DomainCommandMixin:
    """领域命令 / Agent / 供应商 / 图推理（由 ChatService 组合）。"""

    async def _handleClarify(
        self, session: AsyncSession, dto: ChatRequest, pc: _PipelineContext,
    ) -> ChatResponse:
        """CLARIFY：概念解释。不执行 SQL、不更新查询状态，仅记录 usage 与对话。"""
        schemaText = self._nl2sql.buildSchemaText(pc.classes, joins=pc.joins)
        answerResp, answerConfig, (wastedPt, wastedCt) = await self._callWithFallback(
            session, dto.sessionId, pc.configs, pc.selected, "clarify",
            lambda cfg: self._llmFactory(cfg).complete(
                messages=[
                    LlmMessage(role="system", content=_CLARIFY_SYSTEM_PROMPT),
                    LlmMessage(
                        role="user",
                        content=(
                            f"用户问题：{dto.question}\n\n"
                            f"可用的本体元数据：\n{schemaText or '（当前没有可用表结构）'}"
                        ),
                    ),
                ],
                model=cfg.model_name,
            ),
        )
        totalTokens = answerResp.promptTokens + answerResp.completionTokens + wastedPt + wastedCt
        totalCost = self._costFor(answerConfig, answerResp.promptTokens, answerResp.completionTokens)
        totalCost += self._costFor(pc.selected, wastedPt, wastedCt)
        await self._recordUsage(
            session, dto.sessionId, answerConfig,
            answerResp.promptTokens, answerResp.completionTokens, purpose=USAGE_PURPOSE_CLARIFY,
        )
        await self._storeSessionMessages(session, dto.sessionId, dto.question, answerResp.content, None)
        affinity = await self._buildAffinityStatus(
            session, dto.sessionId, answerConfig.id, answerConfig.model_name,
        )
        return ChatResponse(
            answer=answerResp.content,
            intent=IntentType.CLARIFY.value,
            tokensUsed=totalTokens,
            cost=float(totalCost),
            modelName=answerConfig.model_name,
            affinityStatus=affinity,
        )

    # =========================================================================
    # 领域命令（Phase 2）：DEFINE / MAP / METRIC
    # =========================================================================

    async def _handleDomainCommand(
        self, session: AsyncSession, dto: ChatRequest, result: IntentResult
    ) -> ChatResponse:
        """DEFINE / MAP / METRIC：调用本体 CRUD，不进入 NL2SQL 流水线、不消耗 Token。

        分发策略：
        - DEFINE：target 有值（斜杠 /define 创建类）→ _handleDefineClass；
                  否则（自然语言"定义指标"）→ _handleDefineMetric。
        - METRIC：携带 metric+formula（斜杠 /metric）→ 创建指标 _handleDefineMetric；
                  否则（自然语言"有哪些指标"）→ _handleShowMetric 列举。
        - MAP：直接派发。
        """
        if result.intent == IntentType.DEFINE:
            if result.target and not result.metric:
                return await self._handleDefineClass(session, dto, result)
            return await self._handleDefineMetric(session, dto, result)
        if result.intent == IntentType.MAP:
            return await self._handleMapProperty(session, dto, result)
        if result.intent == IntentType.METRIC:
            if result.metric and result.formula:
                return await self._handleDefineMetric(
                    session, dto, result, intentLabel=IntentType.METRIC
                )
            return await self._handleShowMetric(session, dto, result)
        raise AssertionError(f"非领域命令意图: {result.intent}")

    async def _handleSupplier360(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        result: IntentResult,
    ) -> ChatResponse:
        """Phase 5.3：供应商 360° 视图（chat 拦截路径，跳过 NL2SQL）。

        supplierKey 缺失 → 引导文案（澄清如何提问），answer=提示语，
        supplier360=None（前端按字段存在性路由，不渲染卡片）。
        supplierKey 找不到 supplier → 仍返回 ChatResponse + answer=错误说明，
        supplier360=None（与 4.5 ACL 「NotFoundError 通用消息」原则一致：避免泄漏
        「不存在 vs 无权限」侧信道）。
        成功 → answer=中文简短摘要 + supplier360=<完整对象>，前端 MessageItem
        按字段存在性路由到 Supplier360Card 渲染。

        Phase 6.x：result.supplierKey 直接透传 str（THBI '10105' 或 '100001'），
        service.get360 内部 _resolveSupplier 双路解析（先 enterprise_code 后
        enterprise_key），不再做 int() 强制转换。
        """
        if not result.supplierKey:
            return ChatResponse(
                answer=MSG_SCHEMA_CHAT_SUPPLIER_KEY_MISSING,
                intent=result.intent.value,
            )
        try:
            data = await Supplier360Service().get360(session, result.supplierKey)
        except NotFoundError:
            return ChatResponse(
                answer=MSG_SUPPLIER_360_NOT_FOUND.format(key=result.supplierKey),
                intent=result.intent.value,
            )
        answer = (
            f"供应商 {data.profile.enterprise_code}（{result.supplierKey}）360° 视图："
            f"已聚合 {len(data.entity_codes)} 条跨系统编码 + "
            f"{len(data.kpis)} 项 SUPPLIER 特征指标。"
        )
        return ChatResponse(
            answer=answer,
            intent=result.intent.value,
            supplier360=data,
        )

    async def _handleSupplierRisk(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        result: IntentResult,
    ) -> ChatResponse:
        """Phase 5.4：供应商风险 Agent（chat 拦截路径，跳过 NL2SQL）。

        与 _handleSupplier360 同语义：supplierKey 缺失 / 找不到 supplier 都返回 ChatResponse
        + 友好 answer，supplier_risk=None（前端按字段存在性路由，不渲染卡片）。
        成功 → answer=等级 + 主要风险点 + 建议动作，supplier_risk=<完整对象>。
        LLM 不可用由 SupplierRiskService 内部降级到 fallback_template，chat 层无感。

        Phase 6.x：result.supplierKey 直接透传 str（详见 _handleSupplier360 注释）。
        """
        if not result.supplierKey:
            return ChatResponse(
                answer=MSG_SCHEMA_CHAT_SUPPLIER_KEY_MISSING,
                intent=result.intent.value,
            )
        # H9：客户端与单价都必须来自真实 model config。
        # 历史实现只传工厂、服务内部 `llm_factory(None)`：本项目未配置 openaiApiKey
        # 环境变量 ⇒ createClient(None) 恒 None ⇒ 下一行 AttributeError 被 except 吞
        # ⇒ 风险点在生产上从未真正由 LLM 生成过；而那条「活着」的成本分支又硬编码
        # CNY 单价，与台账里其余 USD 行不同口径。两处一并修。
        # 解析失败（无可用配置）→ 两者皆 None ⇒ 服务显式降级到模板且不记账。
        resolved = await self._resolveChatLlmClient(session, dto)
        llm_factory = self._llmFactory if resolved is not None else None
        llm_config = resolved[1] if resolved is not None else None
        try:
            data = await SupplierRiskService().assess(
                session,
                result.supplierKey,
                llm_factory=llm_factory,
                llm_config=llm_config,
            )
        except NotFoundError:
            return ChatResponse(
                answer=MSG_SUPPLIER_RISK_NOT_FOUND.format(key=result.supplierKey),
                intent=result.intent.value,
            )
        # 审查 MEDIUM#2 同型缺口：风险点 LLM 调用此前只计量不落库，这里补写审计
        await self._recordDirectUsage(
            session, dto.sessionId,
            tokens_used=data.tokens_used,
            prompt_tokens=data.prompt_tokens,
            completion_tokens=data.completion_tokens,
            cost=data.cost, model_name=data.llm_model_name,
            purpose=USAGE_PURPOSE_SUPPLIER_RISK,
        )
        # answer 拼装复用 buildRiskAnswer（与 Agent Tool 共用同一文案，DRY）
        answer = buildRiskAnswer(data, result.supplierKey)
        return ChatResponse(
            answer=answer,
            intent=result.intent.value,
            supplier_risk=data,
        )

    async def _handleAgentRun(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        result: IntentResult,
        *,
        user: CurrentUser | None = None,
    ) -> ChatResponse:
        """Phase 6.4：Agent 运行时（chat 拦截路径，跳过 NL2SQL）。

        用户显式指名 Agent（如「用 supplier_risk_agent 评估供应商 100001」）时，
        由 AgentRuntimeService 完成：注册解析 → 状态门禁 → 工具绑定 → 策略拦截 →
        参数抽取 → 执行。成功 → answer=工具返回文案 + agent_run=<完整对象>，
        前端 MessageItem 按字段存在性路由到 AgentResponseCard 渲染。

        失败语义（不向用户抛领域异常，全部转友好 answer + agent_run=None）：
        - Agent 未注册 → MSG_AGENT_NOT_FOUND_BY_CODE（不泄漏「不存在 vs 无权限」侧信道）
        - 不可运行（DRAFT/DEPRECATED/无工具绑定）→ MSG_AGENT_NOT_RUNNABLE
        - 策略拦截（403）→ MSG_AGENT_RUN_DENIED（detail 含真实 data_object + 缺失 data_layer）
        - 参数解析失败（422）→ MSG_AGENT_RUN_BAD_INPUT
        - 其他未预期异常 → log warning + MSG_AGENT_RUN_FAILED（绝不阻断 chat 主链路）

        每次成功运行后持久化对话消息；若工具内部发生了 LLM 调用（tokens>0），
        补写 token_usage 审计（审查 MEDIUM#2 修复：此前只进 DTO 计量、从不落库）。
        """
        agent_code = (result.agent_code or "").strip().upper()
        if not agent_code:
            return ChatResponse(
                answer=MSG_AGENT_RUN_MISSING_CODE,
                intent=result.intent.value,
            )
        # audit-on-finish：finally 确保成功/失败都记录审计（Task 10）
        started_at = datetime.now(timezone.utc)
        actor = user.userId if user is not None else "chat"
        actor_departments = user.departments if user is not None else None
        run_status = AUDIT_RUN_STATUS_SUCCESS
        run = None
        # H9：工具 handler（supplier_risk）需要「工厂 + 真实 config」才能调 LLM 并按
        # config 单价计价。只传工厂时 handler 内部拿不到 config，只能降级模板。
        resolved = await self._resolveChatLlmClient(session, dto)
        try:
            run = await self._agentRuntime.run(
                session,
                agent_code,
                dto.question,
                llm_factory=self._llmFactory if resolved is not None else None,
                llm_config=resolved[1] if resolved is not None else None,
                # 真实调用方身份透传为 actor（归属审计；安全审查 HIGH#1 修复）
                actor=actor,
            )
        except NotFoundError:
            run_status = AUDIT_RUN_STATUS_FAILED
            finished_at = datetime.now(timezone.utc)
            await _audit.record(
                session,
                entity_type=AUDIT_ENTITY_AGENT_RUN_LOG,
                entity_id=0,
                action=AUDIT_ACTION_CREATE,
                actor=actor,
                actor_departments=actor_departments,
                after={
                    "agentCode": agent_code,
                    "status": run_status,
                    "startedAt": started_at.isoformat(),
                    "finishedAt": finished_at.isoformat(),
                    "error": AGENT_RUN_ERROR_AGENT_NOT_FOUND,
                },
            )
            # FAILED 分支不经 persist：审计必须自行提交，否则请求结束随事务回滚丢失
            await session.commit()
            return ChatResponse(
                answer=MSG_AGENT_NOT_FOUND_BY_CODE.format(code=agent_code),
                intent=result.intent.value,
            )
        except PermissionDeniedError as exc:
            run_status = AUDIT_RUN_STATUS_FAILED
            finished_at = datetime.now(timezone.utc)
            await _audit.record(
                session,
                entity_type=AUDIT_ENTITY_AGENT_RUN_LOG,
                entity_id=0,
                action=AUDIT_ACTION_CREATE,
                actor=actor,
                actor_departments=actor_departments,
                after={
                    "agentCode": agent_code,
                    "status": run_status,
                    "startedAt": started_at.isoformat(),
                    "finishedAt": finished_at.isoformat(),
                    "error": AGENT_RUN_ERROR_PERMISSION_DENIED,
                },
            )
            # FAILED 分支不经 persist：审计必须自行提交，否则请求结束随事务回滚丢失
            await session.commit()
            # 用异常自身 message（含真实 data_object），替代硬编码 object="?"（审查 LOW#5）
            return ChatResponse(
                answer=exc.message,
                intent=result.intent.value,
            )
        except ConflictError as exc:
            run_status = AUDIT_RUN_STATUS_FAILED
            finished_at = datetime.now(timezone.utc)
            await _audit.record(
                session,
                entity_type=AUDIT_ENTITY_AGENT_RUN_LOG,
                entity_id=0,
                action=AUDIT_ACTION_CREATE,
                actor=actor,
                actor_departments=actor_departments,
                after={
                    "agentCode": agent_code,
                    "status": run_status,
                    "startedAt": started_at.isoformat(),
                    "finishedAt": finished_at.isoformat(),
                    "error": AGENT_RUN_ERROR_AGENT_NOT_RUNNABLE,
                },
            )
            # FAILED 分支不经 persist：审计必须自行提交，否则请求结束随事务回滚丢失
            await session.commit()
            # 用异常自身 message（含真实原因：无工具绑定 / 状态非 ACTIVE），
            # 替代硬编码 status="inactive"（与上方 PERMISSION_DENIED 分支同口径）
            return ChatResponse(
                answer=exc.message,
                intent=result.intent.value,
            )
        except ValidationError as exc:
            run_status = AUDIT_RUN_STATUS_FAILED
            finished_at = datetime.now(timezone.utc)
            await _audit.record(
                session,
                entity_type=AUDIT_ENTITY_AGENT_RUN_LOG,
                entity_id=0,
                action=AUDIT_ACTION_CREATE,
                actor=actor,
                actor_departments=actor_departments,
                after={
                    "agentCode": agent_code,
                    "status": run_status,
                    "startedAt": started_at.isoformat(),
                    "finishedAt": finished_at.isoformat(),
                    "error": AGENT_RUN_ERROR_BAD_INPUT,
                },
            )
            # FAILED 分支不经 persist：审计必须自行提交，否则请求结束随事务回滚丢失
            await session.commit()
            return ChatResponse(
                answer=exc.message,
                intent=result.intent.value,
            )
        except Exception:  # noqa: BLE001 - Agent 执行降级，不阻断 chat 主链路
            run_status = AUDIT_RUN_STATUS_FAILED
            finished_at = datetime.now(timezone.utc)
            await _audit.record(
                session,
                entity_type=AUDIT_ENTITY_AGENT_RUN_LOG,
                entity_id=0,
                action=AUDIT_ACTION_CREATE,
                actor=actor,
                actor_departments=actor_departments,
                after={
                    "agentCode": agent_code,
                    "status": run_status,
                    "startedAt": started_at.isoformat(),
                    "finishedAt": finished_at.isoformat(),
                    "error": AGENT_RUN_ERROR_UNEXPECTED,
                },
            )
            # FAILED 分支不经 persist：审计必须自行提交，否则请求结束随事务回滚丢失
            await session.commit()
            logger.warning(
                "agent run failed for %s, degrading: %s",
                agent_code, dto.question, exc_info=True,
            )
            return ChatResponse(
                answer=MSG_AGENT_RUN_FAILED,
                intent=result.intent.value,
            )
        finally:
            # audit-on-finish：finally 确保成功/失败都记录审计（Task 10）
            # 注意：FAILED 分支已在 except 块中记录；此处仅处理 SUCCESS 路径
            if run is not None and run_status == AUDIT_RUN_STATUS_SUCCESS:
                finished_at = datetime.now(timezone.utc)
                await _audit.record(
                    session,
                    entity_type=AUDIT_ENTITY_AGENT_RUN_LOG,
                    entity_id=0,
                    action=AUDIT_ACTION_CREATE,
                    actor=actor,
                    actor_departments=actor_departments,
                    after={
                        "agentCode": run.agent_code,
                        "agentName": run.agent_name,
                        "tool": run.tool,
                        "status": run_status,
                        "answer": run.answer,
                        "tokensUsed": run.tokens_used,
                        "promptTokens": run.prompt_tokens,
                        "completionTokens": run.completion_tokens,
                        "cost": run.cost,
                        "llmModelName": run.llm_model_name,
                        "executedAt": run.executed_at.isoformat() if run.executed_at else None,
                        "startedAt": started_at.isoformat(),
                        "finishedAt": finished_at.isoformat(),
                    },
                )
        # 审查 MEDIUM#2：Agent 内部 LLM 调用（如 supplier_risk 生成风险点）补写
        # token_usage 审计（modelConfigId=None：工具路径未透传配置，落已知 modelName + cost）
        await self._recordDirectUsage(
            session, dto.sessionId,
            tokens_used=run.tokens_used,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            cost=run.cost, model_name=run.llm_model_name,
            purpose=USAGE_PURPOSE_AGENT_RUN,
        )
        await self._storeSessionMessages(
            session, dto.sessionId, dto.question, run.answer, None
        )
        return ChatResponse(
            answer=run.answer,
            intent=result.intent.value,
            agent_run=run,
        )

    async def _handleGraphReasoning(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        result: IntentResult,
    ) -> ChatResponse:
        """Phase 6.3：知识图谱多跳推理（chat 拦截路径，跳过 NL2SQL）。

        与 _handleSupplierRisk 同语义：supplierKey 缺失 / 实体不在图中都返回
        ChatResponse + 友好 answer，graph_traversal=None（前端按字段存在性路由）。
        成功 -> answer=可达实体摘要（模板合成，不调 LLM），graph_traversal=<完整对象>。
        Neo4j 异常 -> warn + 降级 answer（不阻断 chat 主链路）。
        """
        if not result.supplierKey:
            return ChatResponse(
                answer=MSG_SCHEMA_CHAT_SUPPLIER_KEY_MISSING,
                intent=result.intent.value,
            )
        try:
            supplierKey = int(result.supplierKey)
        except ValueError:
            return ChatResponse(
                answer=MSG_SCHEMA_CHAT_SUPPLIER_KEY_MISSING,
                intent=result.intent.value,
            )
        try:
            # Phase 7 G3：问句可携带跳数（「3 跳关联」→ 3），None 默认 2，
            # 越界 clamp 到 [1, 5]，不抛 500。
            maxHops = resolveChatMaxHops(result.max_hops)
            traversal = await self._graphTraversal.traverse(
                "Supplier", str(supplierKey), maxHops
            )
            answer = self._graphTraversal.buildChatAnswer(traversal)
        except NotFoundError:
            return ChatResponse(
                answer=MSG_GRAPH_TRAVERSAL_NOT_FOUND.format(
                    label="Supplier", key=supplierKey
                ),
                intent=result.intent.value,
            )
        except Exception:  # noqa: BLE001 - 图库故障降级，不阻断 chat
            logger.warning(
                "graph reasoning failed for supplier %s, degrading",
                supplierKey,
                exc_info=True,
            )
            return ChatResponse(
                answer=MSG_GRAPH_TRAVERSAL_UNAVAILABLE,
                intent=result.intent.value,
            )
        await self._storeSessionMessages(
            session, dto.sessionId, dto.question, answer, None
        )
        return ChatResponse(
            answer=answer,
            intent=result.intent.value,
            graph_traversal=traversal,
        )

    async def _handleDefineMetric(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        result: IntentResult,
        *,
        intentLabel: IntentType = IntentType.DEFINE,
    ) -> ChatResponse:
        """创建本体指标；公式缺失时返回格式引导。

        intentLabel：自然语言"定义指标"→ DEFINE；斜杠 /metric → METRIC。
        """
        if not result.metric or not result.formula:
            answer = MSG_DEFINE_METRIC_GUIDE_PREFIX + MSG_DEFINE_METRIC_GUIDE_EXAMPLE
            return ChatResponse(answer=answer, intent=intentLabel.value)
        metric = await self._ontology.createMetric(
            session, OntologyMetricCreate(metric_name=result.metric, formula=result.formula)
        )
        answer = MSG_METRIC_DEFINED.format(name=metric.metric_name, formula=metric.formula)
        await self._storeSessionMessages(session, dto.sessionId, dto.question, answer, None)
        return ChatResponse(answer=answer, intent=intentLabel.value)

    async def _handleDefineClass(
        self, session: AsyncSession, dto: ChatRequest, result: IntentResult
    ) -> ChatResponse:
        """DEFINE（斜杠 /define）：创建本体类（设计01 语义）。

        result.target = class_name；result.source = alias；result.formula = description
        （复用 IntentResult 字段，语义独立、不冲突）。
        缺类名时返回格式引导。
        """
        if not result.target:
            answer = MSG_DEFINE_CLASS_GUIDE_PREFIX + MSG_DEFINE_CLASS_GUIDE_EXAMPLE
            return ChatResponse(answer=answer, intent=IntentType.DEFINE.value)
        entity = await self._ontology.createClass(
            session,
            OntologyClassCreate(
                class_name=result.target,
                class_alias=result.source,
                description=result.formula,
            ),
        )
        answer = MSG_CLASS_CREATED.format(name=entity.class_name) + (
            MSG_CLASS_ALIAS_SUFFIX.format(alias=entity.class_alias) if entity.class_alias else ""
        ) + (f"：{entity.description}" if entity.description else "")
        await self._storeSessionMessages(session, dto.sessionId, dto.question, answer, None)
        return ChatResponse(answer=answer, intent=IntentType.DEFINE.value)

    async def _handleShowMetric(
        self, session: AsyncSession, dto: ChatRequest, result: IntentResult
    ) -> ChatResponse:
        """METRIC：列举系统已定义的指标（含公式）。"""
        metrics = await self._ontology.listMetrics(session)
        if not metrics:
            answer = MSG_NO_METRICS_DEFINED
        else:
            lines = [f"- {m.metric_name}（{m.formula}）" for m in metrics]
            answer = MSG_METRIC_LIST_HEADER + "\n".join(lines)
        await self._storeSessionMessages(session, dto.sessionId, dto.question, answer, None)
        return ChatResponse(answer=answer, intent=IntentType.METRIC.value)

    async def _handleMapProperty(
        self, session: AsyncSession, dto: ChatRequest, result: IntentResult
    ) -> ChatResponse:
        """MAP：把源属性映射到目标类（设置属性外键 ref_class_id）。"""
        if not result.source or not result.target:
            answer = MSG_MAP_PROPERTY_GUIDE
            return ChatResponse(answer=answer, intent=IntentType.MAP.value)
        classes = await self._ontology.listClasses(session)
        sourceProp = next(
            (
                prop
                for cls in classes
                for prop in cls.properties
                if (
                    prop.property_name == result.source
                    or prop.property_alias == result.source
                    or (prop.business_aliases and result.source in prop.business_aliases)
                )
            ),
            None,
        )
        targetClass = next(
            (
                cls
                for cls in classes
                if cls.class_name == result.target or cls.class_alias == result.target
            ),
            None,
        )
        if sourceProp is None or targetClass is None:
            answer = MSG_MAP_PROPERTY_NOT_FOUND.format(
                source=result.source, target=result.target
            ) + MSG_MAP_PROPERTY_RETRY_HINT
            return ChatResponse(answer=answer, intent=IntentType.MAP.value)
        await self._ontology.updateProperty(
            session,
            sourceProp.id,
            OntologyPropertyUpdate(is_foreign_key=True, ref_class_id=targetClass.id),
        )
        answer = MSG_MAP_PROPERTY_OK.format(
            property=sourceProp.property_name, className=targetClass.class_name
        )
        await self._storeSessionMessages(session, dto.sessionId, dto.question, answer, None)
        return ChatResponse(answer=answer, intent=IntentType.MAP.value)

    async def _streamDomainCommand(
        self, dto: ChatRequest, session: AsyncSession, result: IntentResult
    ) -> AsyncIterator[StreamEvent]:
        """领域命令的 SSE 形式：token(结果) + done（零 LLM 消耗，与闲聊同构）。"""
        resp = await self._handleDomainCommand(session, dto, result)
        yield StreamEvent(EVENT_TOKEN, {"content": resp.answer})
        yield StreamEvent(EVENT_DONE, {"tokensUsed": 0, "cost": 0.0, "modelName": None})

    @staticmethod
    def _entitiesFor(result: IntentResult) -> ExtractedEntities | None:
        """查询意图抽取的结构化实体；闲聊/澄清/领域命令无实体（返回 None）。"""
        if result.intent not in (
            IntentType.QUERY,
            IntentType.NEW_QUERY,
            IntentType.REFINE,
            IntentType.FOLLOW_UP,
        ):
            return None
        if result.dimension is None and result.metric is None and result.chartType is None:
            return None
        return ExtractedEntities(
            dimension=result.dimension, metric=result.metric, chartType=result.chartType
        )
