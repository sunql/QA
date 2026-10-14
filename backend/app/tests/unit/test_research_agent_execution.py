"""研究执行面（`failedStep` / `_DRIVER_ERRORS` / `failedStep` 的错误载荷口径等）单测。

与 integration 套件分工：本套件只测**纯函数与字面量收口**（无 DB 写、无状态机），
端到端执行落库仍在 `app/tests/integration/test_research_agent_service.py`。
"""

from __future__ import annotations

import inspect

from app.services import research_agent_execution


def test_opt_step_index_reused_in_error_payload() -> None:
    """OPT_STEP_INDEX 已是 stepIndex 字符串 SSOT，errorPayload 调用必须复用。

    `failedStep` 里把步错误收敛成 `research.error` 事件，载荷里 `stepIndex=...` 字段名
    是下游 chat / 前端契约的 key。改字面量会与 OPT_STEP_INDEX（= "stepIndex"）漂移，
    收口后必须锁死这里用 OPT_STEP_INDEX（以 `**{OPT_STEP_INDEX: ...}` 形式）。
    """
    src = inspect.getsource(research_agent_execution.failedStep)
    # 变量名 `OPT_STEP_INDEX` 必须出现在源码里（**kwarg 解包形式）
    assert "OPT_STEP_INDEX" in src, (
        f"failedStep 源码里必须引用 OPT_STEP_INDEX（SSOT），"
        f"但搜不到该常量名；当前源码片段:\n{src}"
    )
    # 防御：原裸字面量 `stepIndex=result[...]` 必须被替换掉
    assert 'stepIndex=result' not in src, (
        f"failedStep 仍含裸 `stepIndex=result[...]` 字面量，未替换为 OPT_STEP_INDEX:\n{src}"
    )
    # 防御：常量的字面值（"stepIndex"）不得作为独立 kwarg 名残留 —— 但允许
    # 作为变量名 `OPT_STEP_INDEX` 的子串出现（这里它不在，所以强校验不会误伤）
    assert "stepIndex" not in src, (
        f"failedStep 源码里残留裸字面量 'stepIndex'（OPT_STEP_INDEX 应是唯一来源）:\n{src}"
    )