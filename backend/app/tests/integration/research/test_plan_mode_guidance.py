"""规划阶段真的消费 mode（Task 13）：真实 PG + 完整 HTTP 链路 + 捕获型 planner。

单测只证明「纯函数拼得对」，证明不了**接线**：`state["mode"]` 是 `ResearchRunState`
的**列**（不在 `state_payload` 里），若 `stateToDict` / `rebuildState` 没把它放进
state dict，整个功能就是一个静默 no-op —— 单测全绿、线上毫无差别。故此处从
`POST /sessions` 带 `mode` 起跑，用**捕获型 planner**（记录真实收到的 question）
断言规划器收到的文本：

1. 三 mode 各自收到自己的指引（缺哪个 mode 都会有一条红）；
2. 不带 mode 的会话走 `DEFAULT_MODE`（默认不是 no-op）；
3. **replan 路径**同时保留 feedback 与 mode 指引（防「后加的 mode 覆盖掉 feedback」）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource, User
from app.domain.multi_step_plan import MultiStepPlan, StepPlan
from app.infrastructure.security.crypto import encryptApiKey
from app.services.chart_service import ChartService
from app.services.enterprise_semantic_layer import BusinessObjectRef, ESLExtraction
from app.services.research_agent_phases import _MODE_GUIDANCE, PLAN_FEEDBACK_HEADER
from app.services.research_agent_service import ResearchAgentService
from app.services.research_session_service import ResearchSessionService
from app.services.step_query_planner import StepPlanResult

_BASE = "/api/v1/research"
_QUESTION = "上个月华东区各供应商的收货量"


# ---------------------------------------------------------------------------
# Arrange 辅助
# ---------------------------------------------------------------------------


async def _seedUser(dbSession: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        display_name=username,
        email=None,
        enabled=True,
        password_hash=None,
    )
    dbSession.add(user)
    await dbSession.commit()
    await dbSession.refresh(user)
    return user


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        name=f"plan-mode-test-{uuid.uuid4().hex[:8]}",
        type=DataSourceType.ORACLE,
        host="oracle-test",
        port=1521,
        database_name="THBIDB",
        username="THBI",
        password_encrypted=encryptApiKey("test-password"),
        is_active=True,
        is_default=True,
        oracle_version="19.0.0.0.0",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


@pytest.fixture()
async def authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    await _seedUser(dbSession, "plan-mode-user")
    await _seedDatasource(dbSession)
    return {"X-User-Id": "plan-mode-user"}


# ---------------------------------------------------------------------------
# 状态机替身：确定性 ESL + 记录 question 的 planner + 零产出 runner
# ---------------------------------------------------------------------------


class _FixedEsl:
    """确定性三臂：命中 1 个业务对象、0 冲突。"""

    async def extract(self, question: str, *, intent: object | None = None) -> ESLExtraction:
        return ESLExtraction(
            businessObjects=[
                BusinessObjectRef(
                    classId=1,
                    className="采购订单",
                    sourceTable="THBI.PO_HEADER",
                    matchedAlias="订单",
                    confidence=0.95,
                )
            ],
            metrics=[],
            knowledge=[],
            confidenceByArm={"business_object": 0.95, "metric": 0.0, "knowledge": 0.0},
            conflicts=[],
        )


class _CapturingPlanner:
    """记录 planner **真实收到**的 question（零 LLM）；`plan=None` 走单步语义。"""

    def __init__(self) -> None:
        self.questions: list[str] = []

    async def plan(
        self, question: str, client: Any, modelName: str | None
    ) -> StepPlanResult:
        self.questions.append(question)
        return StepPlanResult(plan=None, prompt_tokens=0, completion_tokens=0)


class _PlanningPlanner:
    """返回**真实** `MultiStepPlan`（驱动 `normalizePlan` 分支，Task 13 fix2 用）。

    `_CapturingPlanner` 返回 `plan=None` ⇒ 走 `singleStepPlan` 回落，其 `originalQuestion`
    本已等于未注入值，看不到 `normalizePlan` 从 planner 的 `original_question` 抄回
    `plan["originalQuestion"]` 这条路径（那正是把注入指引写进存储的路径）。
    """

    def __init__(self) -> None:
        self.questions: list[str] = []

    async def plan(
        self, question: str, client: Any, modelName: str | None
    ) -> StepPlanResult:
        self.questions.append(question)
        step = StepPlan(
            index=0, description="总体量级", sub_question="上月收货总量", aggregation_only=False
        )
        return StepPlanResult(
            plan=MultiStepPlan(steps=(step,), aggregation_hint="", original_question=question),
            prompt_tokens=0,
            completion_tokens=0,
        )


class _NoopRunner:
    """不执行业务 SQL（零网络）；无产出 ⇒ 本用例不关心执行面结果。"""

    async def executeReadonlySql(
        self, session: AsyncSession, sql: str, *, adapter: Any = None
    ) -> list[dict[str, Any]]:
        return []

    async def runVerification(
        self,
        session: AsyncSession,
        hypothesis: Any,
        *,
        adapter: Any = None,
        dialectArgs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"rows": [], "error": None}


class _EmptyModelConfigs:
    async def list(self, session: AsyncSession, activeOnly: bool = True) -> list:
        return []


def _patchService(
    monkeypatch: pytest.MonkeyPatch, planner: _CapturingPlanner, *, autoConfirm: bool
) -> None:
    """后台状态机替身：**同一个** planner 实例跨请求复用（捕获面唯一）。"""

    def _build() -> ResearchAgentService:
        return ResearchAgentService(
            esl=_FixedEsl(),
            sessionService=ResearchSessionService(),
            planner=planner,
            runner=_NoopRunner(),
            chartService=ChartService(),
            modelConfigs=_EmptyModelConfigs(),
            nl2sql=None,
            autoConfirm=autoConfirm,
        )

    monkeypatch.setattr(researchModule, "buildResearchAgentService", _build)


async def _createSession(
    client: AsyncClient, headers: dict[str, str], question: str, *, mode: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"question": question}
    if mode is not None:
        body["mode"] = mode
    resp = await client.post(f"{_BASE}/sessions", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _postTurn(
    client: AsyncClient, headers: dict[str, str], sessionId: str, question: str
) -> Any:
    return await client.post(
        f"{_BASE}/sessions/{sessionId}/turns", json={"question": question}, headers=headers
    )


async def _answer(
    client: AsyncClient, headers: dict[str, str], checkpointId: str, body: dict[str, Any]
) -> Any:
    return await client.post(
        f"{_BASE}/checkpoints/{checkpointId}/answer", json=body, headers=headers
    )


async def _waitForCheckpoint(
    client: AsyncClient, headers: dict[str, str], sessionId: str, phase: str, timeout: float = 3.0
) -> dict[str, Any]:
    """轮询会话详情，直到出现目标 phase 的 pending checkpoint。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        resp = await client.get(f"{_BASE}/sessions/{sessionId}", headers=headers)
        assert resp.status_code == 200, resp.text
        cp = resp.json().get("pendingCheckpoint")
        if cp and cp["phase"] == phase and cp["status"] == "pending":
            return cp
        await asyncio.sleep(0.05)
    raise RuntimeError(f"未在 {timeout}s 内等到 {phase} checkpoint")


# ---------------------------------------------------------------------------
# 1. 三 mode 各自把指引注入规划问题
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["research", "attribution", "compare"])
async def test_planner_receives_mode_guidance(
    client: AsyncClient,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """带了 mode 的会话 ⇒ 规划器收到的 question 含该 mode 的指引（且以原问题开头）。

    autoConfirm=True ⇒ 一口气跑过意图/规划，无需答题；规划器一定被调用过。
    """
    planner = _CapturingPlanner()
    _patchService(monkeypatch, planner, autoConfirm=True)

    session = await _createSession(client, authHeaders, _QUESTION, mode=mode)
    resp = await _postTurn(client, authHeaders, session["id"], _QUESTION)
    assert resp.status_code == 202, resp.text

    assert planner.questions, "规划阶段根本没调用 planner（接线断了）"
    planned = planner.questions[-1]
    assert planned.startswith(_QUESTION), planned
    assert _MODE_GUIDANCE[mode] in planned, planned
    # 计数守卫：指引**恰好注入一次**。`startswith` / `in` 挡不住「双份注入」
    # （`q\n\ng\n\ng` 两者都通过），故必须数出现次数。
    assert planned.count(_MODE_GUIDANCE[mode]) == 1, planned


async def test_default_mode_still_gets_research_guidance(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """不带 mode ⇒ 走 `DEFAULT_MODE`（默认研究型指引也在，不是 no-op）。"""
    planner = _CapturingPlanner()
    _patchService(monkeypatch, planner, autoConfirm=True)

    session = await _createSession(client, authHeaders, _QUESTION)
    resp = await _postTurn(client, authHeaders, session["id"], _QUESTION)
    assert resp.status_code == 202, resp.text

    assert _MODE_GUIDANCE["research"] in planner.questions[-1], planner.questions


# ---------------------------------------------------------------------------
# 2. replan 路径：feedback 与 mode 指引**都在**
# ---------------------------------------------------------------------------


async def test_replan_keeps_feedback_and_mode_guidance(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """计划点 modify（replan）⇒ 规划器同时收到 feedback 与 mode 指引。

    防的正是「后叠加的 mode 指引把先组合的 feedback 覆盖掉」这类顺序错误：
    两者都必须在最终问题文本里。
    """
    planner = _CapturingPlanner()
    _patchService(monkeypatch, planner, autoConfirm=False)

    session = await _createSession(client, authHeaders, _QUESTION, mode="attribution")
    sid = session["id"]
    resp = await _postTurn(client, authHeaders, sid, _QUESTION)
    assert resp.status_code == 202, resp.text

    intentCp = await _waitForCheckpoint(client, authHeaders, sid, "intent")
    confirmed = await _answer(
        client, authHeaders, intentCp["id"], {"action": "confirm", "choice": {}}
    )
    assert confirmed.status_code == 200, confirmed.text
    planCp = await _waitForCheckpoint(client, authHeaders, sid, "planning")

    firstPlanned = planner.questions[-1]
    assert _MODE_GUIDANCE["attribution"] in firstPlanned, firstPlanned

    # 不带 question 键 ⇒ 走 replan 分支（而非「改写问题」重跑 ESL）
    modified = await _answer(
        client,
        authHeaders,
        planCp["id"],
        {"action": "modify", "choice": {"feedback": "按供应商分组对比"}},
    )
    assert modified.status_code == 200, modified.text
    await _waitForCheckpoint(client, authHeaders, sid, "planning")

    replanned = planner.questions[-1]
    assert replanned != firstPlanned, "replan 必须真的重跑 planner"
    assert PLAN_FEEDBACK_HEADER in replanned, replanned
    assert _MODE_GUIDANCE["attribution"] in replanned, replanned
    # 顺序 + 计数守卫：`_stagePlan` 注释称「先组合反馈、再叠加指引」为承重顺序 —— 这里把它
    # 断言成真（此前无人测该顺序），并各自数一次防双份注入。feedback 在 `question` 里先成型，
    # 指引由 `planQuestionForMode` 后追加 ⇒ feedback 必在指引之前。
    assert replanned.count(PLAN_FEEDBACK_HEADER) == 1, replanned
    assert replanned.count(_MODE_GUIDANCE["attribution"]) == 1, replanned
    assert replanned.index(PLAN_FEEDBACK_HEADER) < replanned.index(
        _MODE_GUIDANCE["attribution"]
    ), replanned


# ---------------------------------------------------------------------------
# 3. 落库的 plan["originalQuestion"] 是**未注入**的原问题（Task 13 fix2）
# ---------------------------------------------------------------------------


async def test_persisted_plan_original_question_is_uninjected(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """计划检查点 options 里的 `plan["originalQuestion"]` 是**未注入**的 `_QUESTION`。

    planner 把入参（=已注入指引的文本）记成 `original_question`，`normalizePlan` 再抄进
    `plan["originalQuestion"]` 并随计划检查点 options 落 JSONB。指引是 prompt 脚手架，
    不该进存储 ⇒ `_stagePlan` 在调用点改回未注入的 `question`。用**真实 MultiStepPlan**
    的 planner 驱动 `normalizePlan` 分支（`_CapturingPlanner` 的 `plan=None` 走回落、
    看不见此路径）。
    """
    planner = _PlanningPlanner()
    _patchService(monkeypatch, planner, autoConfirm=False)

    session = await _createSession(client, authHeaders, _QUESTION, mode="attribution")
    sid = session["id"]
    resp = await _postTurn(client, authHeaders, sid, _QUESTION)
    assert resp.status_code == 202, resp.text

    intentCp = await _waitForCheckpoint(client, authHeaders, sid, "intent")
    await _answer(client, authHeaders, intentCp["id"], {"action": "confirm", "choice": {}})
    planCp = await _waitForCheckpoint(client, authHeaders, sid, "planning")

    # 对照组：planner 确实收到了**已注入**指引的问题（证明注入发生）
    assert _MODE_GUIDANCE["attribution"] in planner.questions[-1], planner.questions
    # 但落库的 originalQuestion 是**未注入**的
    original = planCp["options"]["plan"]["originalQuestion"]
    assert original == _QUESTION, original
    assert _MODE_GUIDANCE["attribution"] not in original, original
