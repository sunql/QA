"""会话级并发互斥与幂等键（Task 2）。

真实 PG + 完整 HTTP 链路；状态机注入确定性 fake 管线（零网络 / 零 LLM）。
覆盖三条契约：

1. **互斥**：会话已有进行中的运行（`research_session.active_run_id` 非空）时，
   第二次提交被拒 409，而不是并发起跑两条状态机；
2. **幂等键**：同一 `idempotencyKey` 的重试返回**同一** turnId（不新建轮次）；
3. **终态释放**：会话落终态（done）后释放执行权 —— 新轮次可再次提交；
4. **不可续跑即释放**：检查点过期 ⇒ 会话落 aborted 并释放（否则永久 409 锁死）。

外加 checkpoint 决策的幂等契约（同一 action + choice 的重复提交返回同一响应，
不同决策仍 409）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource, User
from app.domain.multi_step_plan import MultiStepPlan
from app.domain.research_models import ResearchCheckpoint, ResearchSession, ResearchTurn
from app.infrastructure.security.crypto import encryptApiKey
from app.services.chart_service import ChartService
from app.services.enterprise_semantic_layer import (
    BusinessObjectRef,
    ESLExtraction,
)
from app.services.research_agent_service import ResearchAgentService
from app.services.research_run_state import RunStateSeed, buildRunState
from app.services.research_session_service import ResearchSessionService
from app.services.step_query_planner import StepPlanResult

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/research"


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


async def _seedDatasource(dbSession: AsyncSession, *, isDefault: bool = True) -> DataSource:
    ds = DataSource(
        name=f"run-concurrency-test-{uuid.uuid4().hex[:8]}",
        type=DataSourceType.ORACLE,
        host="oracle-test",
        port=1521,
        database_name="THBIDB",
        username="THBI",
        password_encrypted=encryptApiKey("test-password"),
        is_active=True,
        is_default=isDefault,
        oracle_version="19.0.0.0.0",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


@pytest.fixture()
async def authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    await _seedUser(dbSession, "run-concurrency-user")
    await _seedDatasource(dbSession)
    return {"X-User-Id": "run-concurrency-user"}


# ---------------------------------------------------------------------------
# 状态机替身：命中 1 个业务对象（固定 #1 意图点）/ 可选 autoConfirm 跑到 done
# ---------------------------------------------------------------------------


class _FixedEsl:
    """确定性三臂：命中 1 个业务对象、0 冲突 ⇒ 状态机走到意图确认点。"""

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


@dataclass(frozen=True)
class _SqlStep:
    """执行步（duck-type）：真实 `StepPlan` 字段 + 研究链路扩展的 `sql`。"""

    index: int
    description: str
    sub_question: str
    sql: str | None = None
    aggregation_only: bool = False


class _NoPlanPlanner:
    """`plan=None` ⇒ 状态机按单步语义走（非多步问题），确定性暂停在计划点。"""

    async def plan(
        self, question: str, client: Any, modelName: str | None
    ) -> StepPlanResult:
        return StepPlanResult(plan=None, prompt_tokens=0, completion_tokens=0)


class _SqlPlanner:
    """返回**一个带 SQL 的数据步** ⇒ 执行面有产出，状态机可跑到终态 done。

    与 `test_research_agent_service.SqlStep` 同款 duck-type：真实 `StepPlan` 无 `sql`
    字段，执行面只接受「计划里已带 SQL」的步（本测试无 Nl2SqlService）。
    """

    async def plan(self, question: str, client: Any, modelName: str | None) -> Any:
        return MultiStepPlan(
            steps=(
                _SqlStep(
                    index=0, description="收货量", sub_question="近12月收货量", sql="SELECT 1"
                ),
            ),
            aggregation_hint="",
            original_question=question,
        )


class _NoopRunner:
    """不执行业务 SQL（零网络）；执行面产物恒为空 ⇒ 步失败。"""

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


class _OneRowRunner:
    """执行面返回 1 行 ⇒ 步成功 ⇒ 状态机一路跑到 done。"""

    async def executeReadonlySql(
        self, session: AsyncSession, sql: str, *, adapter: Any = None
    ) -> list[dict[str, Any]]:
        return [{"month": "2026-01", "cnt": 100}]

    async def runVerification(
        self,
        session: AsyncSession,
        hypothesis: Any,
        *,
        adapter: Any = None,
        dialectArgs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"rows": [{"cnt": 1}], "error": None}


class _EmptyModelConfigs:
    async def list(self, session: AsyncSession, activeOnly: bool = True) -> list:
        return []


def _service(*, autoConfirm: bool = False, runToDone: bool = False) -> ResearchAgentService:
    """fake 管线 + **真实** ResearchSessionService（落真库）。

    `runToDone=True` 时换成「带 SQL 的步 + 有产出的 runner」⇒ 状态机跑到 done；
    默认（`runToDone=False`）执行面无产出 ⇒ 停在意图点（autoConfirm 下则落 failed）。
    """
    return ResearchAgentService(
        esl=_FixedEsl(),
        sessionService=ResearchSessionService(),
        planner=_SqlPlanner() if runToDone else _NoPlanPlanner(),
        runner=_OneRowRunner() if runToDone else _NoopRunner(),
        chartService=ChartService(),
        modelConfigs=_EmptyModelConfigs(),
        nl2sql=None,
        autoConfirm=autoConfirm,
    )


def _patchService(
    monkeypatch: pytest.MonkeyPatch, *, autoConfirm: bool = False, runToDone: bool = False
) -> None:
    """后台状态机替身：每次 build 都产新实例（状态机无跨请求内存态）。"""
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _service(autoConfirm=autoConfirm, runToDone=runToDone),
    )


async def _createSession(client: AsyncClient, headers: dict[str, str], question: str) -> dict[str, Any]:
    resp = await client.post(f"{_BASE}/sessions", json={"question": question}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _postTurn(
    client: AsyncClient, headers: dict[str, str], sessionId: str, body: dict[str, Any]
) -> Any:
    return await client.post(f"{_BASE}/sessions/{sessionId}/turns", json=body, headers=headers)


async def _waitForCheckpoint(
    client: AsyncClient, headers: dict[str, str], sessionId: str, phase: str, timeout: float = 2.0
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


async def _answerCheckpointsUntilTerminal(
    client: AsyncClient,
    headers: dict[str, str],
    sessionId: str,
    *,
    firstCheckpointId: str | None = None,
    action: str = "confirm",
    maxCheckpoints: int = 6,
) -> str:
    """反复「答掉当前 pending checkpoint → 等下一段」，直到会话落**终态**，返回其状态。

    多步状态机一条主线可以**连续暂停多次**（intent → planning → hypothesis，见
    `research_agent_service` docstring 的「三个固定 checkpoint」）：只答一个点不足以跑到
    `done`。`maxCheckpoints` 是死循环护栏（超出即显式报错，不静默返回中间态）。
    `firstCheckpointId` 供调用方复用已等到的那一个，避免重复轮询。
    """
    pendingId = firstCheckpointId
    for _ in range(maxCheckpoints):
        resp = await client.get(f"{_BASE}/sessions/{sessionId}", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        status = body["session"]["status"]
        if status in ("done", "failed", "aborted"):
            return status
        pending = body.get("pendingCheckpoint")
        if pendingId is None and pending is not None:
            pendingId = pending["id"]
        if pendingId is None:
            await asyncio.sleep(0.05)
            continue
        answered = await client.post(
            f"{_BASE}/checkpoints/{pendingId}/answer",
            json={"action": action, "choice": {}},
            headers=headers,
        )
        assert answered.status_code == 200, answered.text
        pendingId = None
    raise RuntimeError(f"会话未在 {maxCheckpoints} 个 checkpoint 内到达终态")


# ---------------------------------------------------------------------------
# 1. 互斥：进行中的轮次存在 ⇒ 第二次提交 409
# ---------------------------------------------------------------------------


async def test_second_turn_rejected_while_first_awaiting_user(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """首轮暂停在意图点（awaiting_user）时，第二个 submit 必须 409 —— 不允许并发起跑。

    **文案必须说真话**：此时并没有轮次在跑（机器停着等用户决策），故不得回
    「存在进行中的研究轮次」—— 那会误导用户以为在等机器。只说真实原因（待决策检查点）。

    **也不得承诺未接线的动作**：`POST /research/sessions/{id}/abort` 目前**没有前端调用方**
    （删除会话的入口只在 `/research/history` 页，而 409 出现在没有该入口的会话页），
    故文案里**不能**出现「可取消本会话」—— 那是让用户去做一件在眼前这个页面上做不到的事，
    与旧文案同属一类谎言（只是小一点）。abort 端点接上前端后，再在此处公告它。
    """
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]

    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    await _waitForCheckpoint(client, authHeaders, sid, phase="intent")

    r2 = await _postTurn(client, authHeaders, sid, {"question": "Q2"})
    assert r2.status_code == 409, r2.text
    msg = r2.json()["error"]
    assert "检查点" in msg, msg
    assert "等待" in msg, msg
    assert "进行中" not in msg, f"暂停态不得说「有轮次在跑」: {msg}"
    assert "取消" not in msg, f"abort 无前端入口，不得承诺做不到的动作: {msg}"


async def test_second_turn_conflict_message_names_running_round(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真·在跑（running 且已有占用）时仍回「存在进行中的研究轮次」（文案分支的另一半）。

    走服务层直接占坑：ASGITransport 下后台任务会先跑完，HTTP 层造不出「正在跑」的中间态。
    """
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    svc = ResearchSessionService()
    run_state = await _seedRunRow(dbSession, svc, uuid.UUID(sid))
    ok, _ = await svc.claimActiveRun(dbSession, uuid.UUID(sid), run_state.id, None)
    assert ok is True
    await svc.updateSessionStatus(dbSession, uuid.UUID(sid), "running")
    await dbSession.commit()

    r2 = await _postTurn(client, authHeaders, sid, {"question": "Q2"})
    assert r2.status_code == 409, r2.text
    assert "存在进行中的研究轮次" in r2.json()["error"]


# ---------------------------------------------------------------------------
# 2. 幂等键：同一 key 的重试返回同一 turnId
# ---------------------------------------------------------------------------


async def test_idempotency_key_retry_returns_same_turn(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一幂等键重复提交 ⇒ 202 且 turnId 与首次一致（不新建轮次）。"""
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    body = {"question": "Q1", "idempotencyKey": "idem-001"}

    r1 = await _postTurn(client, authHeaders, sid, body)
    assert r1.status_code == 202, r1.text
    r2 = await _postTurn(client, authHeaders, sid, body)

    assert r2.status_code == 202, r2.text
    assert r2.json()["turnId"] == r1.json()["turnId"]


async def test_different_idempotency_key_is_rejected(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """不同幂等键仍是「新的一轮」⇒ 会话被占用时拒绝（幂等键不是并发许可）。"""
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]

    r1 = await _postTurn(
        client, authHeaders, sid, {"question": "Q1", "idempotencyKey": "idem-001"}
    )
    assert r1.status_code == 202, r1.text
    r2 = await _postTurn(
        client, authHeaders, sid, {"question": "Q2", "idempotencyKey": "idem-002"}
    )
    assert r2.status_code == 409, r2.text


# ---------------------------------------------------------------------------
# 3. 终态释放：会话 done 后新轮次可提交
# ---------------------------------------------------------------------------


async def test_active_run_released_when_session_done(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跑到终态 done ⇒ active_run_id 清空 ⇒ 新轮次不被拒。"""
    _patchService(monkeypatch, autoConfirm=True, runToDone=True)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]

    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.json()["session"]["status"] == "done", detail.text

    assert await _activeRunId(dbSession, sid) is None, "终态必须释放 active_run"

    r2 = await _postTurn(client, authHeaders, sid, {"question": "Q2"})
    assert r2.status_code == 202, r2.text


async def test_active_run_released_when_session_failed(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态 failed 同样释放：失败路径上 finally 的释放会被后台 rollback 撤掉，

    必须由收尾重放（`markFailed`）重新落下 —— 否则会话带着 active_run_id 永久 409。
    """
    _patchService(monkeypatch, autoConfirm=True)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]

    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.json()["session"]["status"] == "failed", detail.text

    assert await _activeRunId(dbSession, sid) is None, "终态必须释放 active_run"

    r2 = await _postTurn(client, authHeaders, sid, {"question": "Q2"})
    assert r2.status_code == 202, r2.text


async def test_active_run_released_when_resume_reaches_done(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**resume 段**跑到终态同样释放执行权（与首轮共用 `_guardedRun` 的 finally）。

    只覆盖「首轮 → done」是不够的：用户答完检查点后的第二段才是常态路径，且它与首轮
    走的是同一条收尾释放；漏了它，暂停-答完-再暂停这条主线会留下永久占用。
    """
    _patchService(monkeypatch, runToDone=True)  # 首轮仍暂停在意图点
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")
    assert await _activeRunId(dbSession, sid) is not None, "暂停期间占用执行权"

    finalStatus = await _answerCheckpointsUntilTerminal(
        client, authHeaders, sid, firstCheckpointId=cp["id"]
    )

    assert finalStatus == "done", finalStatus
    assert await _activeRunId(dbSession, sid) is None, "resume 段终态必须释放"

    r2 = await _postTurn(client, authHeaders, sid, {"question": "Q2"})
    assert r2.status_code == 202, r2.text


# ---------------------------------------------------------------------------
# 3.5 主动取消（POST /sessions/{id}/abort）
# ---------------------------------------------------------------------------


async def test_abort_releases_claim_and_publishes_terminal_event(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消停在检查点的会话 ⇒ aborted + 释放执行权 + 发**终态**事件（打开的 SSE 流才关）。

    这是「用户走开」场景的刻意出路：否则占用永不释放、此后每次提交都 409，唯一出口是
    删掉整个会话（连带丢报告）。终态事件复用既有终态码（`turn_failed`）⇒ 无需前端改动。
    """
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    await _waitForCheckpoint(client, authHeaders, sid, phase="intent")
    assert await _activeRunId(dbSession, sid) is not None

    queue = researchModule._bus.subscribe(sid)  # 必须先订阅：总线不做历史回放
    try:
        resp = await client.post(f"{_BASE}/sessions/{sid}/abort", headers=authHeaders)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"sessionStatus": "aborted", "cancelled": True}, resp.text
        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait())
    finally:
        researchModule._bus.unsubscribe(sid, queue)

    assert frames, "abort 必须发事件（`_eventFrames` 只认终态 → 否则已打开的流不关）"
    # 总线项是 (事件名, 载荷, seq) 三元组（Task 12）
    assert any(
        researchModule._isTerminal(event, payload) for event, payload, _seq in frames
    ), frames

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.json()["session"]["status"] == "aborted", detail.text
    assert await _activeRunId(dbSession, sid) is None, "abort 必须释放执行权"

    r2 = await _postTurn(client, authHeaders, sid, {"question": "Q2"})
    assert r2.status_code == 202, r2.text


async def test_abort_done_session_keeps_done(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已 `done` 的会话被 abort ⇒ 幂等无副作用：**状态仍是 done**，不得回退丢结果。

    取消是「别等了」，不是「删结果」；把已完成的研究翻成 aborted 会让用户丢掉报告。
    """
    _patchService(monkeypatch, autoConfirm=True, runToDone=True)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.json()["session"]["status"] == "done", detail.text

    resp = await client.post(f"{_BASE}/sessions/{sid}/abort", headers=authHeaders)
    assert resp.status_code == 200, resp.text
    # 无副作用调用要如实标注：cancelled=false 且状态是**真实**终态（前端据此不误报「已取消」）
    assert resp.json() == {"sessionStatus": "done", "cancelled": False}, resp.text

    after = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert after.json()["session"]["status"] == "done", "abort 不得把终态回退"


async def test_abort_rejected_while_running(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**真在跑**（`running` + 占用在手）时 abort 必须 409 且什么都不动（review Fix A）。

    放宽成「一切非终态」会打断正在执行的状态机，两个后果都打穿本任务：

    1. 状态机的事后写入是**无条件**的（`updateSessionStatus` 盲写，`_runFrom` 落 done /
       `_pauseForUser` 落 awaiting_user）⇒ abort 后它照样写回，用户看到「已取消」、
       刷新又活过来；
    2. abort 顺手清空 `active_run_id` ⇒ 下一个 `POST /turns` 放行、`claimActiveRun` 在
       `IS NULL` 上成功 ⇒ **同一会话两条状态机并行**。

    故断言三件：409、状态不变、占用保留；外加**不得发帧** —— 发终态帧会关掉**活着的**流。
    走服务层直接占坑：ASGITransport 下后台任务会先跑完，HTTP 层造不出「正在跑」的中间态。
    """
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    svc = ResearchSessionService()
    run_state = await _seedRunRow(dbSession, svc, uuid.UUID(sid))
    ok, _ = await svc.claimActiveRun(dbSession, uuid.UUID(sid), run_state.id, None)
    assert ok is True
    await svc.updateSessionStatus(dbSession, uuid.UUID(sid), "running")
    await dbSession.commit()
    claimed = await _activeRunId(dbSession, sid)
    assert claimed is not None

    queue = researchModule._bus.subscribe(sid)
    try:
        resp = await client.post(f"{_BASE}/sessions/{sid}/abort", headers=authHeaders)
        assert resp.status_code == 409, resp.text
        assert "无法取消" in resp.json()["error"], resp.text
        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait())
    finally:
        researchModule._bus.unsubscribe(sid, queue)

    assert frames == [], f"拒绝 abort 不得发帧（会把活着的流关掉）: {frames}"
    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.json()["session"]["status"] == "running", "被拒的 abort 不得改状态"
    assert await _activeRunId(dbSession, sid) == claimed, "被拒的 abort 不得释放执行权"


async def test_abort_terminal_status_has_no_effect_on_failed(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`failed` 终态同样幂等无副作用（不止 `done` 一条被覆盖）。"""
    _patchService(monkeypatch, autoConfirm=True)  # 无产出 ⇒ 落 failed
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.json()["session"]["status"] == "failed", detail.text

    resp = await client.post(f"{_BASE}/sessions/{sid}/abort", headers=authHeaders)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sessionStatus": "failed", "cancelled": False}, resp.text


async def test_abort_then_stale_checkpoint_answer_cannot_resurrect(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """abort 之后，答那张仍 `pending` 的检查点卡片**不得**让会话复活（review Fix F）。

    没有这条守卫时的链条：abort 只翻了会话状态，pending checkpoint 原样留着 ⇒ 迟到的
    「答卡片」把它 resolve 掉、`resumeTurn` 把状态写回 `running` ⇒ **会话复活**；而
    `resumeTurn` **从不认领执行权** ⇒ 复活后的会话是 `running` + `active_run_id IS NULL`
    ⇒ 并发的 `POST /turns` 直接放行、`claimActiveRun` 在 `IS NULL` 上成功
    ⇒ 同一会话两条状态机（正是上一轮刚堵住的洞）。

    断言**库中状态**而非状态码（项目教训：只断状态码是假绿 —— 别的分支也能返同一个码）：
    状态仍 `aborted`、执行权仍为空、checkpoint 仍 `pending`（⇒ 守卫在 `resolveCheckpoint`
    **之前**，一行都没改）、轮次数不变（⇒ 没有起跑）。
    """
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")

    aborted = await client.post(f"{_BASE}/sessions/{sid}/abort", headers=authHeaders)
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["cancelled"] is True, aborted.text
    turnsBefore = await _turnCount(dbSession, sid)

    late = await client.post(
        f"{_BASE}/checkpoints/{cp['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert late.status_code == 409, late.text  # X5 claim invariant guard: abort 已清执行权 ⇒ answer 拒绝（"研究会话未持有执行权…"），替代原先的 200 + SSE 异步拒

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    body = detail.json()
    assert body["session"]["status"] == "aborted", body["session"]
    assert await _activeRunId(dbSession, sid) is None, "执行权不得被复活"
    pending = body["pendingCheckpoint"]
    assert pending is not None, "检查点不得被 resolve（守卫必须在 resolveCheckpoint 之前）"
    assert pending["status"] == "pending", pending
    assert await _turnCount(dbSession, sid) == turnsBefore, "不得写下任何恢复轮次（没有起跑）"


async def test_expired_checkpoint_cleanup_warns_when_not_cancellable(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过期善后遇到「不可取消」的会话必须**告警**，不得静默 no-op（review Fix G）。

    白名单收窄成 `awaiting_user` 之后（Fix A），善后若落在别的非终态上就会变成
    「只留一句 info 日志、执行权继续占着」—— 正是本任务要修的死路，却**没有任何信号**。
    本条把信号钉住：状态非 `awaiting_user` ⇒ 必有一条 WARNING 提到「善后未生效」。

    造法：直接种出 `running` + 持有执行权的会话（ASGITransport 下 HTTP 层造不出
    「正在跑」的中间态），再把 pending checkpoint 的 TTL 拨到过去 ⇒ 后台 resume 先撞
    `CheckpointStaleError`（守卫只挡终态，`running` 不挡）⇒ 走善后分支。
    只断日志**不上抛**是本用例的另一半：善后抛异常会掩盖已发给用户的原始冲突原因。
    """
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    svc = ResearchSessionService()
    run_state = await _seedRunRow(dbSession, svc, uuid.UUID(sid))
    ok, _ = await svc.claimActiveRun(dbSession, uuid.UUID(sid), run_state.id, None)
    assert ok is True
    turn = await svc.appendTurn(dbSession, sessionId=uuid.UUID(sid), role="user", content={})
    cp = await svc.openCheckpoint(
        dbSession,
        sessionId=uuid.UUID(sid),
        turnId=turn.id,
        phase="intent",
        options={"signal": "fixed_scope", "resumePhase": "plan", "prompt": "?"},
        prompt="?",
    )
    await svc.updateSessionStatus(dbSession, uuid.UUID(sid), "running")
    await dbSession.execute(
        update(ResearchCheckpoint)
        .where(ResearchCheckpoint.id == cp.id)
        .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
        .execution_options(synchronize_session=False)
    )
    await dbSession.commit()
    claimed = await _activeRunId(dbSession, sid)

    caplog.set_level(logging.WARNING)
    stale = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert stale.status_code == 200, stale.text

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("善后未生效" in r.getMessage() for r in warnings), [r.getMessage() for r in warnings]
    assert await _activeRunId(dbSession, sid) == claimed, "善后 no-op ⇒ 执行权仍被占着（正是告警的内容）"


async def test_answer_checkpoint_reports_real_session_status(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession, monkeypatch
) -> None:
    """首答 pending checkpoint 的响应必须报**库中真实**状态（review Fix E）。

    此前硬编码 `sessionStatus="running"` —— 那是「状态机接下来要去的状态」，不是此刻的事实：
    此刻会话正 `awaiting_user`（正因为停在这里，才存在这个 pending checkpoint 可答）。
    与幂等回放分支同口径：报真值，前端按库里的事实渲染，而不是按一个猜测。
    """
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")
    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.json()["session"]["status"] == "awaiting_user", detail.text

    answered = await client.post(
        f"{_BASE}/checkpoints/{cp['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["sessionStatus"] == "awaiting_user", answered.text


# ---------------------------------------------------------------------------
# 4. claim / release 语义（服务层，真实 PG）
# ---------------------------------------------------------------------------


async def _seedRunRow(
    dbSession: AsyncSession, svc: ResearchSessionService, sessionId: uuid.UUID
) -> Any:
    """建一条真实 research_run_state 行（active_run_id 的 FK 目标）。"""
    turn = await svc.appendTurn(dbSession, sessionId=sessionId, role="user", content={})
    run_state = buildRunState(
        RunStateSeed(
            session_id=sessionId,
            turn_id=turn.id,
            current_question="q",
            mode="research",
            user_id=None,
            model_id=None,
            datasource_id=None,
        )
    )
    await svc.createRunState(dbSession, run_state)
    await dbSession.flush()
    return run_state


async def test_claim_is_exclusive_and_idempotent_by_key(dbSession: AsyncSession) -> None:
    """第二个 run 被拒（返回占用者 id）；同 runId 重入放行；同幂等键视为同一运行。"""
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=None, question="q")
    first = await _seedRunRow(dbSession, svc, row.id)
    second = await _seedRunRow(dbSession, svc, row.id)

    ok, existing = await svc.claimActiveRun(dbSession, row.id, first.id, None)
    assert (ok, existing) == (True, None)

    # 另一运行 ⇒ 拒绝，并报出占用者
    ok2, existing2 = await svc.claimActiveRun(dbSession, row.id, second.id, None)
    assert ok2 is False
    assert existing2 == first.id

    # 同 runId 重入 ⇒ 通过（同一运行重试认领）
    ok3, existing3 = await svc.claimActiveRun(dbSession, row.id, first.id, None)
    assert (ok3, existing3) == (True, None)

    # 幂等键命中已落库的运行 ⇒ 视为同一运行（不被占用判定拒绝）
    keyed = await _seedRunRow(dbSession, svc, row.id)
    keyed.state_payload = {**keyed.state_payload, "idempotencyKey": "idem-X"}
    await dbSession.flush()
    ok4, hit4 = await svc.claimActiveRun(dbSession, row.id, uuid.uuid4(), "idem-X")
    assert (ok4, hit4) == (True, keyed.id)


async def _activeRunId(dbSession: AsyncSession, sessionId: str | uuid.UUID) -> uuid.UUID | None:
    """读**库中**真实 active_run_id（列查询，绕过 identity map 的陈旧缓存）。"""
    target = sessionId if isinstance(sessionId, uuid.UUID) else uuid.UUID(sessionId)
    return await dbSession.scalar(
        select(ResearchSession.active_run_id).where(ResearchSession.id == target)
    )


async def _turnCount(dbSession: AsyncSession, sessionId: str | uuid.UUID) -> int:
    """库中该会话的轮次数（「有没有真的起跑」的硬证据，比状态码可靠）。"""
    target = sessionId if isinstance(sessionId, uuid.UUID) else uuid.UUID(sessionId)
    return await dbSession.scalar(
        select(func.count()).select_from(ResearchTurn).where(ResearchTurn.session_id == target)
    )


async def test_release_keeps_claim_until_terminal(dbSession: AsyncSession) -> None:
    """非终态（awaiting_user）不释放执行权；落终态才释放。"""
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=None, question="q")
    # 提前取出主键：本用例全程按库中真值断言，不再依赖 ORM 对象属性
    sid = row.id
    run_state = await _seedRunRow(dbSession, svc, sid)

    ok, _ = await svc.claimActiveRun(dbSession, sid, run_state.id, None)
    assert ok is True
    await dbSession.flush()
    assert await _activeRunId(dbSession, sid) == run_state.id, "认领必须落到会话行"
    # 认领走的是条件 UPDATE（synchronize_session=False）：ORM 对象上的值可能是陈旧的，
    # 故断言一律走 _activeRunId 的列查询（认领/释放各自负责同步回对象，见服务实现）
    assert row.active_run_id == run_state.id, "认领必须同步回同会话的 ORM 对象"

    await svc.updateSessionStatus(dbSession, sid, "awaiting_user")
    await svc.releaseActiveRun(dbSession, sid, run_state.id)
    assert await _activeRunId(dbSession, sid) == run_state.id, (
        "等待用户期间不得释放（否则并发轮次会溜进来）"
    )

    await svc.updateSessionStatus(dbSession, sid, "done")
    await svc.releaseActiveRun(dbSession, sid, run_state.id)
    assert await _activeRunId(dbSession, sid) is None
    assert row.active_run_id is None, "释放必须同步回同会话的 ORM 对象"


# ---------------------------------------------------------------------------
# 5. checkpoint 决策幂等
# ---------------------------------------------------------------------------


async def test_checkpoint_answer_identical_replay_is_idempotent(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 (action, choice) 的重复提交 ⇒ 幂等 200（决策不被重新应用），不再是 409。

    幂等的判据是**决策没被再应用一次**（200 + 同一 `nextPhase`），不是响应体逐字相同：
    `sessionStatus` 是**当前**会话状态的快照 —— 首次提交时后台刚要起跑故报 running，
    回放时机器已在下一点暂停 ⇒ 如实报 awaiting_user（硬编码 running 才是说谎）。
    """
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")

    body = {"action": "confirm", "choice": {}}
    first = await client.post(f"{_BASE}/checkpoints/{cp['id']}/answer", json=body, headers=authHeaders)
    assert first.status_code == 200, first.text

    replay = await client.post(f"{_BASE}/checkpoints/{cp['id']}/answer", json=body, headers=authHeaders)
    assert replay.status_code == 200, replay.text
    assert replay.json()["nextPhase"] == first.json()["nextPhase"]

    # 回放报的是**当前**会话状态（非首次提交那一刻的快照）
    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert replay.json()["sessionStatus"] == detail.json()["session"]["status"]
    assert replay.json()["sessionStatus"] == "awaiting_user", detail.text


async def test_expired_checkpoint_aborts_session_and_releases_claim(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """checkpoint 过期 ⇒ 会话落 aborted 且释放执行权（review 高危项回归守卫）。

    为什么必须：过期的检查点再也无法 resolve，会话也就永远跑不到终态 ⇒
    `releaseActiveRun` 的终态闸门永不打开 ⇒ `active_run_id` 永久占用 ⇒ 用户此后
    每次提交都 409，而唯一出路是删掉整个会话（连带丢报告）。锁死是本次变更最坏的
    后果，故这条路径必须有守卫。

    **同时钉住第二个调用点的前提**（review Fix A）：`_abortExpiredSession` 调用的
    `abortSessionIfActive` 现在只接受 `awaiting_user`。这条路径上会话**确实**停在
    `awaiting_user`（过期是后台 resume 时才发现的，而 `resumeTurn` 里的
    `updateSessionStatus(running)` 排在 `resolveCheckpoint` 之后、且异常会回滚掉），
    故下面的显式断言不是装饰 —— 哪天它不再是 awaiting_user，本用例会先报出来，
    而不是让善后变成静默 no-op（那样占用又永久留着）。
    """
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")
    assert await _activeRunId(dbSession, sid) is not None, "跑到暂停应留下执行权"
    paused = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert paused.json()["session"]["status"] == "awaiting_user", paused.text

    # 把 TTL 拨到过去（等价于「用户 24h 后才点提交」），无需等待真实过期
    await dbSession.execute(
        update(ResearchCheckpoint)
        .where(ResearchCheckpoint.id == uuid.UUID(cp["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
        .execution_options(synchronize_session=False)
    )
    await dbSession.commit()

    # 端点同步返 200（过期是后台 resume 时才发现的），冲突经 SSE 传回
    stale = await client.post(
        f"{_BASE}/checkpoints/{cp['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert stale.status_code == 200, stale.text

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.json()["session"]["status"] == "aborted", detail.text
    assert await _activeRunId(dbSession, sid) is None, "过期分支必须释放执行权"

    # 关键：不再永久 409 —— 新轮次可提交（否则锁死，用户只能删会话）
    r2 = await _postTurn(client, authHeaders, sid, {"question": "Q2"})
    assert r2.status_code == 202, r2.text


async def test_checkpoint_replay_with_nonempty_choice_is_idempotent(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**非空 choice** 的回放同样幂等（真实的「改一下再点一次」/ modify 带反馈）。

    空 choice 是退化情形（`{} == {}` 谁比都对）：只有非空 choice 才能证明判据真的是
    「逐字比对 (action, user_choice)」，而不是「只要都非 pending 就放行」。
    """
    _patchService(monkeypatch, runToDone=True)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")

    body = {
        "action": "modify",
        "choice": {"question": "只看甲供应商近12月收货量", "feedback": "限定供应商"},
    }
    first = await client.post(f"{_BASE}/checkpoints/{cp['id']}/answer", json=body, headers=authHeaders)
    assert first.status_code == 200, first.text

    replay = await client.post(f"{_BASE}/checkpoints/{cp['id']}/answer", json=body, headers=authHeaders)
    assert replay.status_code == 200, replay.text
    assert replay.json()["nextPhase"] == first.json()["nextPhase"]

    # 反例：同一 checkpoint 换个非空 choice ⇒ 409（非空也不等于放行）
    conflict = await client.post(
        f"{_BASE}/checkpoints/{cp['id']}/answer",
        json={"action": "modify", "choice": {"question": "只看乙供应商近12月收货量"}},
        headers=authHeaders,
    )
    assert conflict.status_code == 409, conflict.text
    assert "已决策" in conflict.json()["error"]


async def test_checkpoint_answer_conflicting_decision_still_409(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复提交但**决策不同**（reject ≠ confirm）⇒ 仍 409（幂等不放行改写）。"""
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")

    first = await client.post(
        f"{_BASE}/checkpoints/{cp['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert first.status_code == 200, first.text

    conflicting = await client.post(
        f"{_BASE}/checkpoints/{cp['id']}/answer",
        json={"action": "reject", "choice": {}},
        headers=authHeaders,
    )
    assert conflicting.status_code == 409, conflicting.text


async def test_idempotency_key_not_leaked_into_other_sessions(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """幂等键作用域是**会话内**：另一会话用同一 key 仍会新建轮次。"""
    _patchService(monkeypatch)
    sid1 = (await _createSession(client, authHeaders, "Q1"))["id"]
    sid2 = (await _createSession(client, authHeaders, "Q2"))["id"]

    r1 = await _postTurn(client, authHeaders, sid1, {"question": "Q1", "idempotencyKey": "shared"})
    assert r1.status_code == 202, r1.text
    r2 = await _postTurn(client, authHeaders, sid2, {"question": "Q2", "idempotencyKey": "shared"})
    assert r2.status_code == 202, r2.text
    assert r2.json()["turnId"] != r1.json()["turnId"]


async def test_active_run_marker_is_set_while_running(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态机跑起来后会话行上留下 active_run_id（互斥判据的唯一来源）。"""
    _patchService(monkeypatch)
    session = await _createSession(client, authHeaders, "Q1")
    sid = session["id"]
    r1 = await _postTurn(client, authHeaders, sid, {"question": "Q1"})
    assert r1.status_code == 202, r1.text
    await _waitForCheckpoint(client, authHeaders, sid, phase="intent")

    assert await _activeRunId(dbSession, sid) is not None
