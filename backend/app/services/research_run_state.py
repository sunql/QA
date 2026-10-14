"""ResearchRunState 不可变构造器。

与 DB 解耦：buildRunState / updateRunState 只产出新的 ORM 对象，不 flush/commit。
调用方（ResearchAgentService）负责把对象加入 session 并在事务边界提交。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.domain.research_models import ResearchRunState


@dataclass(frozen=True)
class RunStateSeed:
    """创建 ResearchRunState 所需的最小不可变种子。"""

    session_id: uuid.UUID
    turn_id: uuid.UUID
    current_question: str
    mode: str
    user_id: int | None
    model_id: int | None
    datasource_id: int | None
    parent_run_id: uuid.UUID | None = None


def buildRunState(seed: RunStateSeed) -> ResearchRunState:
    """从 turn 入口创建新的 ResearchRunState（不操作 DB）。"""
    payload: dict[str, Any] = {}
    if seed.user_id is not None:
        payload["userId"] = seed.user_id
    return ResearchRunState(
        id=uuid.uuid4(),
        session_id=seed.session_id,
        turn_id=seed.turn_id,
        parent_run_id=seed.parent_run_id,
        current_question=seed.current_question,
        mode=seed.mode,
        scope_version=1,
        datasource_id=seed.datasource_id,
        schema_version=1,
        model_id=seed.model_id,
        state_payload=payload,
    )


def updateRunState(
    state: ResearchRunState,
    *,
    current_question: str | None = None,
    scope_version: int | None = None,
    state_payload: dict[str, Any] | None = None,
) -> ResearchRunState:
    """返回更新后的新 ResearchRunState（不可变、版本化快照）。

    生成新 UUID，原行作为 parent_run_id 链接，保证同一 turn 可保留完整版本历史，
    同时避免 SQLAlchemy identity map 冲突（同 session 内不允许两个同 PK 实例）。
    """
    return ResearchRunState(
        id=uuid.uuid4(),
        session_id=state.session_id,
        turn_id=state.turn_id,
        parent_run_id=state.id,
        current_question=current_question if current_question is not None else state.current_question,
        mode=state.mode,
        scope_version=scope_version if scope_version is not None else state.scope_version,
        datasource_id=state.datasource_id,
        schema_version=state.schema_version,
        model_id=state.model_id,
        state_payload=state_payload if state_payload is not None else state.state_payload,
    )


def stateToDict(state: ResearchRunState) -> dict[str, Any]:
    """将 runState 转为本轮状态机的初始/恢复态字典。"""
    return {
        "runId": str(state.id),
        "question": state.current_question,
        "mode": state.mode,
        "userId": state.state_payload.get("userId"),
        "modelId": state.model_id,
        "datasourceId": state.datasource_id,
        "scopeVersion": state.scope_version,
        "schemaVersion": state.schema_version,
        "parentRunId": str(state.parent_run_id) if state.parent_run_id else None,
    }
