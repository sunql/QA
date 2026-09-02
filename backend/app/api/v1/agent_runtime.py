"""Agent 运行时 REST API（Phase 6.4 feat-agent-runtime-mvp）。

``POST /api/v1/agents/{agent_code}/run``：按 agent_code 调度一次 Agent 运行
（注册解析 → 状态门禁 → 工具绑定 → AgentAccessPolicy 拦截 → 参数抽取 → 执行）。

失败语义由全局 DomainError handler 映射：
- 404 Agent 不存在（NotFoundError）
- 409 不可运行（DRAFT / DEPRECATED / 无工具绑定 / tool 未注册 → ConflictError）
- 403 策略拦截（FORBIDDEN / 无读取策略 → PermissionDeniedError）
- 422 参数缺失（ValidationError）

鉴权：仅 ``getCurrentUser``（与 supplier-360 / supplier-risk 同策略，MVP 不叠加
owner-based ACL；策略语义由 AgentAccessPolicy deny-by-default 承载）。调用方身份
（``_user.userId``）透传为 actor，供工具上下文归属审计（安全审查 HIGH#1 修复）。

安全加固（security-reviewer）：端点挂 ``@limiter.limit(rateLimitValue)``（防 DoS 放大）；
未预期基础设施异常 → 记日志 + 500 友好消息（不泄漏堆栈，领域异常保持原状态码）。

MVP 约定：REST run 不注入 llm_factory（llm_factory=None → supplier_risk 内部降级
fallback_template，输出确定性、可测）；chat 路径注入真实 factory。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.error_messages import MSG_AGENT_RUN_FAILED
from app.domain.exceptions import DomainError
from app.domain.schemas import AgentRunRead, AgentRunRequest
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.agent_runtime_service import AgentRuntimeService
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

_audit = AuditService()

router = APIRouter()


def getAgentRuntimeService() -> AgentRuntimeService:
    return AgentRuntimeService()


@router.post(
    "/{agent_code}/run",
    response_model=AgentRunRead,
    status_code=status.HTTP_200_OK,
    summary="Agent 运行（注册 → 策略拦截 → 工具执行）",
)
@limiter.limit(rateLimitValue)
async def runAgent(
    request: Request,
    agent_code: str,
    payload: AgentRunRequest,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: AgentRuntimeService = Depends(getAgentRuntimeService),
) -> AgentRunRead:
    """按 agent_code 调度一次 Agent 运行，返回 AgentRunRead（含工具结果 + Token 计量）。

    调用方身份（_user.userId）透传为 actor，供工具上下文归属审计；
    未预期基础设施异常 → 记录上下文 + 500 友好消息（领域异常仍由全局 handler 映射）。
    audit-on-finish：finally 确保成功/失败都记录审计（Task 10）。
    """
    started_at = datetime.now(timezone.utc)
    actor = _user.userId
    actor_departments = _user.departments or None
    run_status = "SUCCESS"
    run_result: AgentRunRead | None = None
    try:
        run_result = await service.run(
            db, agent_code, payload.input, actor=actor,
        )
    except DomainError:
        # 领域异常（404/409/403/422）保持状态码，交由全局 DomainError handler 映射
        run_status = "FAILED"
        finished_at = datetime.now(timezone.utc)
        await _audit.record(
            db,
            entity_type="agent_run_log",
            entity_id=0,
            action="CREATE",
            actor=actor,
            actor_departments=actor_departments,
            after={
                "agentCode": agent_code,
                "status": run_status,
                "startedAt": started_at.isoformat(),
                "finishedAt": finished_at.isoformat(),
                "error": "DOMAIN_ERROR",
            },
        )
        raise
    except Exception:  # noqa: BLE001 - 基础设施失败降级为友好 500，不泄漏堆栈
        run_status = "FAILED"
        finished_at = datetime.now(timezone.utc)
        await _audit.record(
            db,
            entity_type="agent_run_log",
            entity_id=0,
            action="CREATE",
            actor=actor,
            actor_departments=actor_departments,
            after={
                "agentCode": agent_code,
                "status": run_status,
                "startedAt": started_at.isoformat(),
                "finishedAt": finished_at.isoformat(),
                "error": "INFRA_ERROR",
            },
        )
        logger.exception(
            "agent run failed for %s: %s", agent_code, payload.input[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_AGENT_RUN_FAILED,
        ) from None
    finally:
        if run_result is not None and run_status == "SUCCESS":
            finished_at = datetime.now(timezone.utc)
            await _audit.record(
                db,
                entity_type="agent_run_log",
                entity_id=0,
                action="CREATE",
                actor=actor,
                actor_departments=actor_departments,
                after={
                    "agentCode": run_result.agent_code,
                    "agentName": run_result.agent_name,
                    "tool": run_result.tool,
                    "status": run_status,
                    "answer": run_result.answer,
                    "tokensUsed": run_result.tokens_used,
                    "promptTokens": run_result.prompt_tokens,
                    "completionTokens": run_result.completion_tokens,
                    "cost": run_result.cost,
                    "llmModelName": run_result.llm_model_name,
                    "executedAt": run_result.executed_at.isoformat() if run_result.executed_at else None,
                    "startedAt": started_at.isoformat(),
                    "finishedAt": finished_at.isoformat(),
                },
            )
