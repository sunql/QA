"""编排 run 的**只读映射**：ORM 行 → 读模型 / 审计 payload。

为什么单独成模块
----------------
``agent_orchestration_run_service`` 负责 run 的**生命周期**（创建、恢复、
取消）；而「这一行 run 对外长什么样」是另一件事：它既被 API 的读模型消费，
也被审计 payload 消费，还必须保证两处口径一致（同一个 run，API 与审计看到
的字段必须同源）。把这层纯映射从生命周期服务里拆出来，生命周期服务就只留
状态迁移，纯映射也就能被单独读懂与复用。

安全约束
--------
审计 payload **绝不含**模型 api key / provider 凭据 / ``handler_ref`` 等
工具配置内部信息；只含 run 自身公开字段。快照字段做深拷贝，避免调用方
后续改动原对象时污染已经构造好的 payload。
"""

from __future__ import annotations

import copy
from typing import Any

from app.domain.agent_orchestration_models import (
    AgentOrchestrationRun,
    AgentOrchestrationStep,
)
from app.domain.agent_orchestration_schemas import (
    AgentRunRead,
    AgentRunStepRead,
)
from app.services.agent_orchestration_types import (
    CostSource,
    PauseCode,
    RunStatus,
    StepKind,
    StepStatus,
)


def runToRead(run: AgentOrchestrationRun) -> AgentRunRead:
    """ORM run → 读模型。"""
    return AgentRunRead(
        run_id=run.run_id,
        agent_code=run.agent_code,
        question=run.question,
        created_by=run.created_by,
        model_config_id=run.model_config_id,
        status=RunStatus(run.status),
        instructions_snapshot=run.instructions_snapshot,
        tool_bindings_snapshot=list(run.tool_bindings_snapshot or []),
        max_iterations=run.max_iterations,
        max_tool_calls=run.max_tool_calls,
        max_cost_usd=float(run.max_cost_usd),
        timeout_seconds=run.timeout_seconds,
        iteration_count=run.iteration_count,
        tool_call_count=run.tool_call_count,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        total_tokens=run.total_tokens,
        cost=float(run.cost),
        cost_source=CostSource(run.cost_source),
        cost_complete=run.cost_complete,
        final_answer=run.final_answer,
        pause_code=PauseCode(run.pause_code) if run.pause_code else None,
        pause_detail=run.pause_detail,
        error_code=run.error_code,
        error_message=run.error_message,
        cancel_requested=run.cancel_requested,
        created_time=run.created_time,
        updated_time=run.updated_time,
        queued_at=run.queued_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        revision=run.revision,
    )


def stepToRead(step: AgentOrchestrationStep) -> AgentRunStepRead:
    """ORM step → 读模型。"""
    return AgentRunStepRead(
        step_no=step.step_no,
        iteration_no=step.iteration_no,
        kind=StepKind(step.kind),
        status=StepStatus(step.status),
        action=step.action,
        plan_summary=step.plan_summary,
        tool_name=step.tool_name,
        input_json=step.input_json,
        output_json=step.output_json,
        answer=step.answer,
        error_code=step.error_code,
        error_message=step.error_message,
        prompt_tokens=step.prompt_tokens,
        completion_tokens=step.completion_tokens,
        cost=float(step.cost),
        started_at=step.started_at,
        finished_at=step.finished_at,
    )


def runToAuditDict(run: AgentOrchestrationRun) -> dict[str, Any]:
    """审计 payload：只含 run 自身公开字段。

    绝不含模型 api key / provider 凭据 / handler_ref 等工具配置内部信息。
    """
    return {
        "runId": run.run_id,
        "agentCode": run.agent_code,
        "status": run.status,
        "modelConfigId": run.model_config_id,
        "question": run.question,
        "instructionsSnapshot": run.instructions_snapshot,
        "toolBindingsSnapshot": copy.deepcopy(run.tool_bindings_snapshot or []),
        "maxIterations": run.max_iterations,
        "maxToolCalls": run.max_tool_calls,
        "maxCostUsd": float(run.max_cost_usd),
        "timeoutSeconds": run.timeout_seconds,
        "iterationCount": run.iteration_count,
        "toolCallCount": run.tool_call_count,
        "promptTokens": run.prompt_tokens,
        "completionTokens": run.completion_tokens,
        "totalTokens": run.total_tokens,
        "cost": float(run.cost),
        "costSource": run.cost_source,
        "costComplete": run.cost_complete,
        "finalAnswer": run.final_answer,
        "pauseCode": run.pause_code,
        "pauseDetail": copy.deepcopy(run.pause_detail),
        "errorCode": run.error_code,
        "errorMessage": run.error_message,
        "cancelRequested": run.cancel_requested,
        "revision": run.revision,
    }
