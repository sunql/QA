"""续跑决策矩阵：端点与 `resumeTurn` 必须给出同一个相位（M3）。

纯函数测试：`decideResume` 只读 checkpoint 的 `phase` / `options`，不碰 DB、不碰 LLM，
故用 `SimpleNamespace` 造桩即可（真库口径由 test_research_api.py 的端点用例覆盖）。
键名一律走常量，不写字面量 —— 本任务的意义就是消掉第二份判定。

末条是**源码结构守卫**（不驱动状态机）：`resumeTurn` 必须**调用** `decideResume`，
否则它会重长回自己那份内联判定 —— 两份判定等价 ⇒ 行为测试全部照绿，第二事实源静默复辟。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.research_agent_phases import decideResume
from app.services.research_agent_ports import (
    ACTION_MODIFY,
    ACTION_REJECT,
    MAX_REPLAN_PER_TURN,
    OPT_ABORT_PHASE,
    OPT_REPLAN_COUNT,
    OPT_RESUME_PHASE,
    PHASE_ESL,
    PHASE_EXECUTE,
    PHASE_PLAN,
    PHASE_REPORT,
)


def _checkpoint(phase: str, **options) -> SimpleNamespace:
    return SimpleNamespace(phase=phase, options=options, session_id="s-1")


def test_rewritten_question_beats_everything() -> None:
    cp = _checkpoint("planning", **{OPT_RESUME_PHASE: PHASE_EXECUTE, OPT_REPLAN_COUNT: 0})
    decision = decideResume(cp, ACTION_MODIFY, {"question": "  换成按供应商看  "})
    assert (decision.phase, decision.isReplan) == (PHASE_ESL, False)


def test_reject_at_dynamic_checkpoint_goes_to_abort_phase() -> None:
    cp = _checkpoint("low_confidence", **{OPT_ABORT_PHASE: PHASE_REPORT})
    decision = decideResume(cp, ACTION_REJECT, None)
    assert (decision.phase, decision.isReplan) == (PHASE_REPORT, False)


def test_planning_modify_under_cap_replans() -> None:
    cp = _checkpoint("planning", **{OPT_RESUME_PHASE: PHASE_EXECUTE, OPT_REPLAN_COUNT: 0})
    decision = decideResume(cp, ACTION_MODIFY, {"instruction": "只看华东"})
    assert (decision.phase, decision.isReplan) == (PHASE_PLAN, True)


@pytest.mark.parametrize("replanCount", [MAX_REPLAN_PER_TURN, MAX_REPLAN_PER_TURN + 1])
def test_planning_modify_at_cap_advances(replanCount: int) -> None:
    cp = _checkpoint(
        "planning", **{OPT_RESUME_PHASE: PHASE_EXECUTE, OPT_REPLAN_COUNT: replanCount}
    )
    decision = decideResume(cp, ACTION_MODIFY, {"instruction": "只看华东"})
    assert (decision.phase, decision.isReplan) == (PHASE_EXECUTE, False)


def test_planning_confirm_advances_without_replan() -> None:
    cp = _checkpoint("planning", **{OPT_RESUME_PHASE: PHASE_EXECUTE, OPT_REPLAN_COUNT: 0})
    decision = decideResume(cp, "confirm", None)
    assert (decision.phase, decision.isReplan) == (PHASE_EXECUTE, False)


# ---------------------------------------------------------------------------
# 结构守卫：源码文本，不驱动状态机
# 与 M1（`test_research_replan_count_ssot.py`）/ M4
# （`test_research_agent_ports.py::test_model_resolution_seam_lives_only_in_new_module`）
# 同款：钉住「只有一份判定」这件事**本身**。
# ---------------------------------------------------------------------------

_SERVICE_SOURCE = Path(__file__).resolve().parents[2] / "services" / "research_agent_service.py"


def _functionSource(text: str, name: str) -> str:
    """取指定函数的源码片段（含定义行到结尾行）；找不到即失败。"""
    lines = text.splitlines()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"未找到函数 {name}：源码结构变了，本守卫需同步更新")


def test_resume_turn_delegates_phase_decision_to_decide_resume() -> None:
    """`resumeTurn` 必须**调用** `decideResume` —— 恢复相位判定只有这一份（M3）。

    判别点：把 `resumeTurn` 里的 `decision = decideResume(...)` 换回它自己的内联判定
    （读 `options["resumePhase"]` / `replanCount`）⇒ 本条立刻红；而全部行为测试仍绿
    （两份判定等价），这正是第二事实源能静默复辟的窗口。
    """
    body = _functionSource(_SERVICE_SOURCE.read_text(encoding="utf-8"), "resumeTurn")
    assert "decideResume(" in body, (
        "resumeTurn 未调用 decideResume：恢复决策 SSOT 被破坏（又长出第二份判定）"
    )
