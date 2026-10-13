"""Task 10+11：进程内持久任务台账 ``research_task`` + 启动回收（reaper）。

真实 PG + 完整 HTTP 链路。承重语义（本 Task 的唯一价值）：

**今天崩溃 / 重启会让会话的 ``active_run_id`` 永久占用** ⇒ 用户此后每次提交都 409，
且 ``abort`` 对 ``running`` 也 409（唯一出路是删会话）。台账 + 启动回收正好堵这个洞。

场景：
1. 入队 + 终态（HTTP 链路）：``POST /sessions/{id}/turns`` ⇒ 有 ``research_task`` 行
   （``kind='turn'``），内联跑完后 ``status='done'``；
2. 认领幂等（服务层，真实 PG 会话）：连认领两次 ⇒ ``True`` 然后 ``False``，``attempt`` 仍 1；
3. **reaper 堵死路（核心，端到端）**：造「崩在跑中」的会话（``running`` + ``active_run_id``
   指向真实 run_state + 一条 ``leased`` task）⇒ 回收后 task ``orphaned``、会话 ``failed``、
   ``active_run_id IS NULL`` ⇒ 紧接着 ``POST /turns`` 必须 202（不再是 409）；
   **反向守卫**：删掉 reaper 里的 ``failSessionIfActive`` 那步 ⇒ 本用例必失败（仍 409）；
4. 不误伤已终态会话：``done`` 会话 + 陈旧 ``leased`` task ⇒ 会话**仍 ``done``**，task 仍 orphaned；
5. reaper 幂等：连调两次 ⇒ 第二次返回空、无额外写入；
6. ``failSessionIfActive`` 三态：``running`` ⇒ True 且落 failed/释放；终态 ⇒ False 且不动。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource
from app.domain.research_models import ResearchRunState, ResearchTask
from app.infrastructure.security.crypto import encryptApiKey
from app.models.rbac import User
from app.services.research_session_service import ResearchSessionService
from app.services.research_task_service import (
    TASK_KIND_RESUME,
    TASK_KIND_TURN,
    claimTask,
    enqueueTask,
    reapOrphanedTasks,
)
from app.tests.integration.research.test_run_concurrency import (
    _patchService,
    _waitForCheckpoint,
)
from app.tests.integration.test_research_stream import _fullService

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/research"
_QUESTION = "供应商收货量为什么下降"


# ---------------------------------------------------------------------------
# 夹具（镜子 test_event_outbox.py）
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        name=f"research-task-oracle-{uuid.uuid4().hex[:8]}",
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
    return ds


@pytest.fixture()
async def authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    dbSession.add(
        User(
            username="research-task-user",
            display_name="task",
            email=None,
            enabled=True,
            password_hash=None,
        )
    )
    await dbSession.commit()
    await _seedDatasource(dbSession)
    return {"X-User-Id": "research-task-user"}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _createSession(client: AsyncClient, headers: dict[str, str]) -> str:
    created = await client.post(
        f"{_BASE}/sessions", json={"question": _QUESTION}, headers=headers
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _postTurn(client: AsyncClient, headers: dict[str, str], sid: str) -> int:
    accepted = await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": _QUESTION}, headers=headers
    )
    return accepted.status_code


async def _taskRows(dbSession: AsyncSession, sid: str) -> list[dict]:
    """直查 ``research_task``（独立于生产读取器 —— 真交叉检验）。

    **刻意用 raw SQL**：认领 / reaper 走 ``synchronize_session=False`` 的条件 UPDATE，
    identity map 里的 ORM 对象会停在旧值（'pending'）；只有从库里读才是真值。
    """
    result = await dbSession.execute(
        text(
            "SELECT id, kind, status, turn_id, attempt, worker_id, error, payload "
            "FROM research_task WHERE session_id = :sid ORDER BY created_at ASC, id ASC"
        ),
        {"sid": sid},
    )
    return [dict(row._mapping) for row in result.all()]


async def _taskRow(dbSession: AsyncSession, taskId: uuid.UUID) -> dict:
    result = await dbSession.execute(
        text(
            "SELECT id, kind, status, turn_id, attempt, worker_id, error "
            "FROM research_task WHERE id = :tid"
        ),
        {"tid": taskId},
    )
    return dict(result.one()._mapping)


async def _sessionStatusAndRun(dbSession: AsyncSession, sid: str) -> tuple[str, uuid.UUID | None]:
    """独立于生产读取器直查会话行（真交叉检验）。"""
    row = (
        await dbSession.execute(
            text("SELECT status, active_run_id FROM research_session WHERE id = :sid"),
            {"sid": sid},
        )
    ).one()
    return str(row[0]), row[1]


async def _crashMidRun(dbSession: AsyncSession, sid: str) -> ResearchTask:
    """造「进程崩在跑中」：running + active_run_id 指向真实 run_state + 一条 leased task。

    task 走真实 ``enqueueTask`` + ``claimTask`` 路径（认领即 leased），然后**不 finish** ——
    正是崩溃留下的形态。
    """
    sidUuid = uuid.UUID(sid)
    mode = await dbSession.scalar(
        text("SELECT mode FROM research_session WHERE id = :sid"), {"sid": sid}
    )
    turn = await researchModule._sessionService.appendTurn(
        dbSession, sessionId=sidUuid, role="user", content={"question": _QUESTION}
    )
    runState = ResearchRunState(session_id=sidUuid, turn_id=turn.id, mode=mode)
    dbSession.add(runState)
    await dbSession.commit()
    await dbSession.execute(
        text(
            "UPDATE research_session SET status = 'running', active_run_id = :rid "
            "WHERE id = :sid"
        ),
        {"rid": runState.id, "sid": sid},
    )
    await dbSession.commit()
    task = await enqueueTask(dbSession, sessionId=sidUuid, turnId=turn.id, kind=TASK_KIND_TURN)
    await dbSession.commit()
    assert await claimTask(dbSession, taskId=task.id, workerId="crashed") is True
    await dbSession.commit()
    return task


# ---------------------------------------------------------------------------
# 1. 入队 + 终态（HTTP 链路）
# ---------------------------------------------------------------------------


async def test_turn_enqueues_task_and_marks_done(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /turns``（202）⇒ 有 ``research_task`` 行、``kind='turn'``，跑完 ``status='done'``。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _createSession(client, authHeaders)
    assert await _postTurn(client, authHeaders, sid) == 202

    rows = await _taskRows(dbSession, sid)
    assert len(rows) == 1, f"turn 应恰好入队一条台账: {len(rows)}"
    assert rows[0]["kind"] == TASK_KIND_TURN
    assert rows[0]["status"] == "done", rows[0]["status"]
    assert rows[0]["turn_id"] is not None


# ---------------------------------------------------------------------------
# 2. resume 路径：答检查点 ⇒ 入队 resume 台账并跑完（守 answerCheckpoint 的 self-commit）
# ---------------------------------------------------------------------------


async def test_checkpoint_answer_enqueues_and_finishes_resume_task(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """答检查点 ⇒ 入队 ``kind='resume'`` 台账，并在同一条请求链里跑到 ``done``。

    **承重（brief §1.5-1）**：``answerCheckpoint`` 此前**没有任何 commit**（``getDb`` 只在
    异常时 rollback），而 ``decideResume`` 是纯函数 ⇒ 入队是该 handler 的**第一处写**。
    若不在 handler 里自己 commit，后台 ``_resumeTurnInBackground`` 自开的会话**看不到**
    task 行 ⇒ ``claimTask`` 恒 False ⇒ 台账永远停在 ``pending`` —— 正是本用例的反向守卫
    （删掉 ``answerCheckpoint`` 里的 ``await db.commit()`` ⇒ 本用例必失败）。
    """
    _patchService(monkeypatch)
    sid = await _createSession(client, authHeaders)
    assert await _postTurn(client, authHeaders, sid) == 202
    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")

    answered = await client.post(
        f"{_BASE}/checkpoints/{cp['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert answered.status_code == 200, answered.text

    rows = await _taskRows(dbSession, sid)
    resumeRows = [r for r in rows if r["kind"] == TASK_KIND_RESUME]
    assert len(resumeRows) == 1, f"答一次检查点应恰好入队一条 resume 台账: {rows}"
    assert resumeRows[0]["status"] == "done", resumeRows[0]
    assert resumeRows[0]["turn_id"] is not None
    assert resumeRows[0]["payload"]["checkpointId"] == cp["id"], resumeRows[0]["payload"]


# ---------------------------------------------------------------------------
# 3. 认领幂等（服务层，真实 PG 会话）
# ---------------------------------------------------------------------------


async def test_claim_task_is_idempotent(
    client: AsyncClient, dbSession: AsyncSession, authHeaders: dict[str, str]
) -> None:
    """同一 ``taskId`` 连续认领两次 ⇒ True 然后 False；``attempt`` 仍为 1（未重复执行）。"""
    sid = await _createSession(client, authHeaders)
    task = await enqueueTask(
        dbSession, sessionId=uuid.UUID(sid), turnId=None, kind=TASK_KIND_TURN
    )
    await dbSession.commit()

    first = await claimTask(dbSession, taskId=task.id, workerId="w1")
    await dbSession.commit()
    second = await claimTask(dbSession, taskId=task.id, workerId="w2")
    await dbSession.commit()

    assert first is True
    assert second is False, "同一 task 不得被重复认领（否则单元重复执行）"
    attempt = await dbSession.scalar(
        select(ResearchTask.attempt).where(ResearchTask.id == task.id)
    )
    assert attempt == 1, f"重复认领不得加 attempt: {attempt}"


# ---------------------------------------------------------------------------
# 3. reaper 堵死路（核心，端到端）
# ---------------------------------------------------------------------------


async def test_reaper_frees_crashed_session_end_to_end(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回收「崩在跑中」的会话 ⇒ 台账 orphaned + 会话 failed/释放 ⇒ ``POST /turns`` 恢复可用。

    这是本 Task 的用户可见价值：崩过一次之后，用户不再被永久 409 锁死。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _createSession(client, authHeaders)
    task = await _crashMidRun(dbSession, sid)
    beforeStatus, beforeRun = await _sessionStatusAndRun(dbSession, sid)
    assert beforeStatus == "running" and beforeRun is not None, (beforeStatus, beforeRun)
    # 前置：崩溃态下新提交确实被挡住（证明「死路」真实存在，用例不形同虚设）
    assert await _postTurn(client, authHeaders, sid) == 409

    orphaned = await reapOrphanedTasks(
        dbSession, sessionService=ResearchSessionService()
    )
    await dbSession.commit()

    assert uuid.UUID(sid) in orphaned, f"被回收的会话应含 {sid}: {orphaned}"
    orphanRow = await _taskRow(dbSession, task.id)
    assert orphanRow["status"] == "orphaned", orphanRow["status"]
    assert orphanRow["error"], "orphan 必须带原因（可观测）"

    afterStatus, afterRun = await _sessionStatusAndRun(dbSession, sid)
    assert afterStatus == "failed", f"崩溃会话应落 failed 释放执行权: {afterStatus}"
    assert afterRun is None, f"active_run_id 必须清空，否则仍是死路: {afterRun}"

    # 端到端：现在新提交必须被接受（不再是 409）—— 这就是本 Task 的价值
    assert await _postTurn(client, authHeaders, sid) == 202


# ---------------------------------------------------------------------------
# 4. 不误伤已终态会话
# ---------------------------------------------------------------------------


async def test_reaper_does_not_downgrade_terminal_session(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``done`` 会话 + 陈旧 ``leased`` task ⇒ 会话**仍 ``done``**（不得翻成 failed），task 仍 orphaned。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _createSession(client, authHeaders)
    sidUuid = uuid.UUID(sid)
    task = await enqueueTask(dbSession, sessionId=sidUuid, turnId=None, kind=TASK_KIND_TURN)
    await dbSession.commit()
    assert await claimTask(dbSession, taskId=task.id, workerId="crashed") is True
    await dbSession.execute(
        text("UPDATE research_session SET status = 'done' WHERE id = :sid"), {"sid": sid}
    )
    await dbSession.commit()

    await reapOrphanedTasks(dbSession, sessionService=ResearchSessionService())
    await dbSession.commit()

    status, _run = await _sessionStatusAndRun(dbSession, sid)
    assert status == "done", f"已终态会话不得被翻成 failed（丢报告）: {status}"
    orphanRow = await _taskRow(dbSession, task.id)
    assert orphanRow["status"] == "orphaned"


# ---------------------------------------------------------------------------
# 5. reaper 幂等
# ---------------------------------------------------------------------------


async def test_reaper_is_idempotent(
    client: AsyncClient, dbSession: AsyncSession, authHeaders: dict[str, str]
) -> None:
    """连调两次 ⇒ 第二次返回空（没有 ``leased`` 行了）、无额外写入。"""
    sid = await _createSession(client, authHeaders)
    task = await _crashMidRun(dbSession, sid)

    first = await reapOrphanedTasks(dbSession, sessionService=ResearchSessionService())
    await dbSession.commit()
    second = await reapOrphanedTasks(dbSession, sessionService=ResearchSessionService())
    await dbSession.commit()

    assert first == [uuid.UUID(sid)]
    assert second == [], f"第二次回收必须为空（幂等）: {second}"
    orphanRow = await _taskRow(dbSession, task.id)
    assert orphanRow["status"] == "orphaned"


# ---------------------------------------------------------------------------
# 6. failSessionIfActive 三态
# ---------------------------------------------------------------------------


async def test_fail_session_if_active_only_touches_running(
    client: AsyncClient, dbSession: AsyncSession, authHeaders: dict[str, str]
) -> None:
    """``running`` ⇒ True 且落 failed/释放；``done``/``aborted`` ⇒ False 且状态与执行权**都不动**。"""
    svc = ResearchSessionService()

    # --- running：命中 ---
    runningSid = await _createSession(client, authHeaders)
    task = await _crashMidRun(dbSession, runningSid)  # 复用：把会话置 running + 占用执行权
    assert task is not None
    assert await svc.failSessionIfActive(dbSession, uuid.UUID(runningSid)) is True
    await dbSession.commit()
    status, run = await _sessionStatusAndRun(dbSession, runningSid)
    assert status == "failed" and run is None, (status, run)
    # 幂等：再调一次为 False（已终态）
    assert await svc.failSessionIfActive(dbSession, uuid.UUID(runningSid)) is False

    # --- done / aborted：不命中、不动 ---
    for terminal in ("done", "aborted"):
        sid = await _createSession(client, authHeaders)
        await dbSession.execute(
            text("UPDATE research_session SET status = :st WHERE id = :sid"),
            {"st": terminal, "sid": sid},
        )
        await dbSession.commit()
        assert await svc.failSessionIfActive(dbSession, uuid.UUID(sid)) is False, terminal
        await dbSession.commit()
        status, _run = await _sessionStatusAndRun(dbSession, sid)
        assert status == terminal, f"终态会话不得被改动: {terminal} -> {status}"
