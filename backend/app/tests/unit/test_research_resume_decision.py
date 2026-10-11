"""续跑决策矩阵：端点与 `resumeTurn` 必须给出同一个相位（M3）。

纯函数测试：`decideResume` 只读 checkpoint 的 `phase` / `options`，不碰 DB、不碰 LLM，
故用 `SimpleNamespace` 造桩即可（真库口径由 test_research_api.py 的端点用例覆盖）。
键名一律走常量，不写字面量 —— 本任务的意义就是消掉第二份判定。
"""

from types import SimpleNamespace

import pytest

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
from app.services.research_agent_phases import decideResume


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
