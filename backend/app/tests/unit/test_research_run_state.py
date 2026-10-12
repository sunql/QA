"""research_run_state 构造器单测：不可变语义与版本化快照契约。

纯构造面测试（零 DB）：buildRunState / updateRunState / stateToDict 不操作 session，
这里只断言字段映射、parent 链接与「原对象不被改动」。
"""

from __future__ import annotations

import uuid

from app.domain.research_models import ResearchRunState
from app.services.research_run_state import RunStateSeed, buildRunState, stateToDict, updateRunState


def _seed(**overrides: object) -> RunStateSeed:
    base: dict[str, object] = {
        "session_id": uuid.uuid4(),
        "turn_id": uuid.uuid4(),
        "current_question": "分析2025年采购总额",
        "mode": "research",
        "user_id": 7,
        "model_id": 42,
        "datasource_id": 3,
    }
    return RunStateSeed(**{**base, **overrides})  # type: ignore[arg-type]


def test_build_run_state_maps_seed_fields() -> None:
    seed = _seed()
    state = buildRunState(seed)

    assert isinstance(state, ResearchRunState)
    assert state.session_id == seed.session_id
    assert state.turn_id == seed.turn_id
    assert state.current_question == seed.current_question
    assert state.mode == seed.mode
    assert state.model_id == 42
    assert state.datasource_id == 3
    assert state.state_payload == {"userId": 7}
    assert state.parent_run_id is None
    assert state.scope_version == 1
    assert state.schema_version == 1


def test_build_run_state_stores_user_id_in_payload() -> None:
    """userId 走 state_payload（恢复态 rebuildState 从 payload 取回）。"""
    state = buildRunState(_seed(user_id=None))
    assert state.state_payload == {}


def test_update_run_state_creates_new_versioned_row() -> None:
    """updateRunState 产新行（新 UUID + parent 链接），原行不动（不可变）。"""
    original = buildRunState(_seed())
    updated = updateRunState(original, current_question="改成2024年订单数量")

    assert updated.id != original.id
    assert updated.parent_run_id == original.id  # 版本链：新行指向旧行
    assert updated.current_question == "改成2024年订单数量"
    assert original.current_question == "分析2025年采购总额"  # 原对象未被改动
    # 未指定字段逐项沿用
    assert updated.session_id == original.session_id
    assert updated.turn_id == original.turn_id
    assert updated.mode == original.mode
    assert updated.model_id == original.model_id
    assert updated.scope_version == original.scope_version


def test_update_run_state_keeps_question_when_not_given() -> None:
    original = buildRunState(_seed())
    updated = updateRunState(original, state_payload={"replanCount": 1})

    assert updated.current_question == original.current_question
    assert updated.state_payload == {"replanCount": 1}
    assert original.state_payload == {"userId": 7}  # payload 原样保留在原行


def test_state_to_dict_shape() -> None:
    parent = buildRunState(_seed())
    child = updateRunState(parent, current_question="q2", scope_version=2)
    payload = stateToDict(child)

    assert payload["runId"] == str(child.id)
    assert payload["question"] == "q2"
    assert payload["mode"] == "research"
    assert payload["userId"] == 7
    assert payload["modelId"] == 42
    assert payload["datasourceId"] == 3
    assert payload["scopeVersion"] == 2
    assert payload["schemaVersion"] == 1
    assert payload["parentRunId"] == str(parent.id)


def test_state_to_dict_parent_run_id_none_for_root() -> None:
    payload = stateToDict(buildRunState(_seed()))
    assert payload["parentRunId"] is None
