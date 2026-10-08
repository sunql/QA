"""ReportPlanner：mode 模板 / LLM 失败降级 / 数据值不被 LLM 改写。

Task 6 契约测试。持久化输入（session / turn / finding）一律落**真实 PostgreSQL**
（unit/conftest.py 的 `dbSession`），LLM 全 fake（零网络）。

`fakeReportDeps` 返回 `(planner, sessionId, turnId)`；测试按 brief 以
`compose(None, ...)` 调用——`None` 走 planner 的 `sessionFactory` 兜底（生产恒传真
session，见 task-6-report.md「偏差」）。
"""

from __future__ import annotations

import pytest

from app.services.report_planner import ReportPlanner
from app.services.research_session_service import ResearchSessionService

QUESTION = "供应商收货量为什么下降"
CLAIM = "供应商A供货减少"
# 与 fake step data 逐字一致：verify 阶段把行数据落进 supporting_data["rows"]
FINDING_ROWS = [{"month": "2026-01", "cnt": 100}]


class FakeLlm:
    """确定性 LLM：返回一句解读文本；`fail=True` 模拟调用异常。"""

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[list] = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        if self._fail:
            raise RuntimeError("llm down")
        return type("R", (), {"content": "解读：指标下降明显。"})()


@pytest.fixture
async def fakeReportDeps(dbSession):
    """真库写 1 session + 1 turn + 1 finding（含行数据），返回 (planner, sessionId, turnId)。"""
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=1, question=QUESTION)
    turn = await svc.appendTurn(
        dbSession, sessionId=row.id, role="user", content={"question": QUESTION}
    )
    await svc.saveFinding(
        dbSession,
        sessionId=row.id,
        turnId=turn.id,
        claimText=CLAIM,
        supportingSql="SELECT 1 AS cnt",
        supportingData={"rowCount": 1, "error": None, "rows": [dict(r) for r in FINDING_ROWS]},
        confidence=0.72,
    )
    await dbSession.commit()
    planner = ReportPlanner(
        sessionService=svc,
        llmClientFactory=lambda: FakeLlm(),
        sessionFactory=lambda: dbSession,
    )
    return planner, row.id, turn.id


@pytest.mark.asyncio
async def test_research_mode_section_order(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    payload, md = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    kinds = [s["kind"] for s in payload["sections"]]
    assert kinds[0] == "executive_summary" and kinds[-1] == "methodology"
    assert any(b["type"] == "chart" for s in payload["sections"] for b in s["blocks"])
    assert "# " in md  # rendered_md 非空


@pytest.mark.asyncio
async def test_chart_data_not_llm_touched(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    chart_blocks = [b for s in payload["sections"] for b in s["blocks"] if b["type"] == "chart"]
    assert chart_blocks[0]["content"]["rows"][0]["cnt"] == 100  # 与 fake step data 逐字一致


@pytest.mark.asyncio
async def test_llm_failure_degrades_to_template(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    planner._llmClientFactory = lambda: FakeLlm(fail=True)
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    summary = payload["sections"][0]["blocks"][0]["content"]
    assert "结构化摘要" in summary


@pytest.mark.asyncio
async def test_unknown_mode_raises(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    with pytest.raises(ValueError):
        await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="nope")


# ---------------------------------------------------------------------------
# 附加：三 mode 模板 / 归档 / 数字免疫（brief 4 条之外的加固）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("attribution", ["conclusion", "hypothesis_table", "data", "alternative", "methodology"]),
        ("compare", ["comparison_table", "data", "diff_analysis", "methodology"]),
    ],
)
async def test_mode_templates(fakeReportDeps, mode: str, expected: list[str]) -> None:
    planner, sessionId, turnId = fakeReportDeps
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode=mode)
    assert [s["kind"] for s in payload["sections"]] == expected


@pytest.mark.asyncio
async def test_compose_is_pure_assembly_no_report_rows(fakeReportDeps, dbSession) -> None:
    """compose 只装配不归档（归档在调用方 `_stageReport`）：报告表不因 compose 新增行。"""
    planner, sessionId, turnId = fakeReportDeps
    payload, md = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    assert await planner._sessions.listReports(dbSession, sessionId) == []
    assert payload["sessionId"] == str(sessionId) and payload["turnId"] == str(turnId)
    assert payload["findingsRef"][0]["claim"] == CLAIM
    assert "| 假设 |" not in md  # research 模式不含假设验证表


@pytest.mark.asyncio
async def test_llm_prompt_excludes_raw_rows(fakeReportDeps) -> None:
    """数字免疫：喂给 LLM 的 prompt 不含原始行数据（100 只出现在 chart 块）。"""
    planner, sessionId, turnId = fakeReportDeps
    fake = FakeLlm()
    planner._llmClientFactory = lambda: fake
    payload, md = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    promptText = "\n".join(m.content for messages in fake.calls for m in messages)
    assert CLAIM in promptText and "100" not in promptText
    chart = next(
        b for s in payload["sections"] for b in s["blocks"] if b["type"] == "chart"
    )
    assert chart["content"]["rows"][0]["cnt"] == 100
    assert chart["sourceRefs"][0]["kind"] == "finding"
    assert "100" in md  # 数字在 MD 里逐字来自 chart 块渲染
