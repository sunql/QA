"""会话级预算只读闸门（Task 15）：判定契约（注入预算 + 假计量器，零 DB）。

只钉**判定纯逻辑**：预算取值 / `<= 0` 关闭 / `Decimal` 比较 / 读成本失败 best-effort /
终止文案可读。接线（相位边界真终止 + 真 SSE）在
`app/tests/integration/research/test_session_budget_gate.py`。

**硬约束**（SSOT：memory `qa-system-no-model-override`）：闸门**只**终止本轮，**绝不**
选模型 / 换模型 / 降级模型 —— 本文件不断言任何模型选择行为，因为此处不存在。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.services.research_agent_ports import SessionBudgetExceeded
from app.services.research_agent_service import (
    SESSION_BUDGET_EXCEEDED_MESSAGE,
    ResearchAgentService,
    sessionBudgetMessage,
)


class _FakeTokenUsage:
    """计量 fake：`getSessionCost` 返回注入成本；`broken=True` 时抛错（模拟计量打嗝）。"""

    def __init__(self, cost: Decimal = Decimal("0"), *, broken: bool = False) -> None:
        self.cost = cost
        self.broken = broken
        self.calls = 0

    async def getSessionCost(self, session: Any, sessionId: str) -> Decimal:
        self.calls += 1
        if self.broken:
            raise RuntimeError("计量查询打嗝")
        return self.cost


def _service(*, sessionBudget: float | None, tokenUsage: _FakeTokenUsage) -> ResearchAgentService:
    """构造期零 DB：协作者全传替身（同 `test_research_model_selection._makeService`）。"""
    return ResearchAgentService(
        esl=object(),
        sessionService=object(),
        planner=object(),
        runner=object(),
        chartService=object(),
        reporter=object(),
        tokenUsage=tokenUsage,
        sessionBudget=sessionBudget,
    )


# ---------------------------------------------------------------------------
# 1. 超/欠预算判定
# ---------------------------------------------------------------------------


async def test_cost_at_budget_trips_the_gate() -> None:
    """成本 == 预算即触发（`>=` 口径，与路由器降级同界）。"""
    svc = _service(sessionBudget=0.1, tokenUsage=_FakeTokenUsage(Decimal("0.1")))
    assert await svc._sessionBudgetExceeded(object(), uuid.uuid4()) is True


async def test_cost_below_budget_does_not_trip() -> None:
    svc = _service(sessionBudget=0.1, tokenUsage=_FakeTokenUsage(Decimal("0.0999999")))
    assert await svc._sessionBudgetExceeded(object(), uuid.uuid4()) is False


async def test_comparison_stays_decimal_and_is_not_degraded_to_float() -> None:
    """金额比较全程 `Decimal`：退化到 float 会把「略欠预算」误判为「恰好达预算」。

    `Decimal("0.2999999999999999999999") < Decimal("0.3")` ⇒ 不触发；但
    `float(...) >= 0.3` 因两侧舍入到**同一个 double** 而变 True ⇒ 若实现退化成 float，
    本用例必失败。
    """
    svc = _service(
        sessionBudget=0.3,
        tokenUsage=_FakeTokenUsage(Decimal("0.2999999999999999999999")),
    )
    assert await svc._sessionBudgetExceeded(object(), uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# 2. `<= 0` ⇒ 关闭闸门
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [0.0, -1.0, -0.000001])
async def test_budget_zero_or_negative_disables_the_gate(budget: float) -> None:
    """预算 `<= 0` ⇒ 恒 False（**即使成本已很高**）—— 语义是「关闭闸门」。

    反向守卫：删掉 `_sessionBudgetExceeded` 里的 `budget <= 0` 早退分支，本用例必失败
    （成本 999 ≥ 预算 0 ⇒ 会返回 True）。
    """
    usage = _FakeTokenUsage(Decimal("999.0"))
    svc = _service(sessionBudget=budget, tokenUsage=usage)

    assert await svc._sessionBudgetExceeded(object(), uuid.uuid4()) is False
    assert usage.calls == 0, "闸门关闭时不得查询成本（早退）"


# ---------------------------------------------------------------------------
# 3. best-effort：读成本失败 ⇒ False
# ---------------------------------------------------------------------------


async def test_cost_lookup_failure_is_best_effort() -> None:
    """读成本抛异常 ⇒ False（计量打嗝不得杀研究）。"""
    svc = _service(sessionBudget=0.1, tokenUsage=_FakeTokenUsage(broken=True))
    assert await svc._sessionBudgetExceeded(object(), uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# 4. 终止文案可读（带真实成本与预算，定点表示）
# ---------------------------------------------------------------------------


def test_budget_message_carries_real_amounts_without_scientific_notation() -> None:
    """文案带真实成本/预算；小数用**定点**（杜绝 `1E-2` 这类科学计数法）。"""
    msg = sessionBudgetMessage(Decimal("0.013"), Decimal("0.01"))

    assert "0.013000" in msg
    assert "0.010000" in msg
    assert "E-" not in msg and "e-" not in msg
    assert "成本" in msg and "预算" in msg


async def test_budget_message_reads_real_amounts_or_falls_back() -> None:
    """终止文案：正常路径带真实金额；二次读打嗝走兜底文案（仍显式给处置，不谎报 0）。"""
    ok = _service(sessionBudget=0.1, tokenUsage=_FakeTokenUsage(Decimal("0.5")))
    message = await ok._sessionBudgetMessage(object(), uuid.uuid4())
    assert "0.500000" in message and "0.100000" in message

    broken = _service(sessionBudget=0.1, tokenUsage=_FakeTokenUsage(broken=True))
    assert await broken._sessionBudgetMessage(object(), uuid.uuid4()) == (
        SESSION_BUDGET_EXCEEDED_MESSAGE
    )


def test_budget_exception_is_a_runtime_error_mentioning_termination_not_model_swap() -> None:
    """异常类型是 RuntimeError（`_guardedRun` 的兜底能接住）；docstring 明说「终止≠换模型」。"""
    assert issubclass(SessionBudgetExceeded, RuntimeError)
    doc = SessionBudgetExceeded.__doc__ or ""
    assert "终止" in doc
    assert "换模型" in doc or "模型" in doc
