"""research REST API（真实 PG + 完整 HTTP 链路）。

覆盖 Task 7 契约：
- 鉴权：无身份 401（研究域要求可归属身份）、router 级 auth 强制；
- 越权：他人会话一律 404（不泄露存在性）；
- 边界：非法 mode / 非法 action 422（Pydantic Literal，Task 3 Minor#3 + Task 6 F5）；
- pendingCheckpoint 的 ``prompt`` 从 ``options["prompt"]`` 派生（Task 3 裁定 #1）；
- turn 202 + 状态机后台跑完（BackgroundTasks 在 ASGI 调用内完成，测试确定性）；
- 报告版本端点（当前 published / 指定版本 / 版本列表 / 缺版本 404）；
- 默认构造点走真实 `ReportPlanner` + 真 `createClient`（Task 6 F1 / Task 6.5 M3 的显式断言）。

后台状态机在测试里注入「空 scope」fake 管线：确定性、零网络、零 LLM，
但**仍然走真实 HTTP + 真实 PG + 真实 ResearchSessionService**（非假绿）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource, LlmConfig
from app.domain.research_models import (
    ResearchCheckpoint,
    ResearchReport,
    ResearchRunState,
    ResearchSession,
    ResearchTurn,
)
from app.services.research_run_state import RunStateSeed, buildRunState
from app.domain.research_schemas import CheckpointAnswerRequest
from app.infrastructure.llm.factory import createClient
from app.infrastructure.security.crypto import encryptApiKey
from app.models.rbac import User
from app.services.chart_service import ChartService
from app.services.enterprise_semantic_layer import (
    EmptyResearchScopeError,
    EnterpriseSemanticLayer,
)
from app.services.messages_zh import MSG_DATASOURCE_NONE_AVAILABLE
from app.services.nl2sql_service import Nl2SqlService
from app.services.report_planner import ReportPlanner
from app.services.research_agent_ports import OPT_REPLAN_COUNT
from app.services.research_agent_service import ResearchAgentService
from app.services.research_session_service import ResearchSessionService

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/research"


# ---------------------------------------------------------------------------
# fixtures：两个真实 DB 用户（stub header 解析到不同 dbUserId）
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


async def _userId(dbSession: AsyncSession, username: str) -> int:
    return await dbSession.scalar(select(User.id).where(User.username == username))


async def _seedDatasource(
    dbSession: AsyncSession, name: str = "api-test-oracle", *, isDefault: bool = True
) -> DataSource:
    """种一个**启用的业务数据源**（Task 13e：建会话必须能解析到业务源）。

    Oracle 形态与生产（`THBI Oracle`）同口径；本文件的 runner 是 `_NoopRunner`，
    适配器只被构造、从不建连，故 host 可以是不可达的假名。
    """
    ds = DataSource(
        name=name,
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
    """用户 A 的 stub 头（DB 命中 → dbUserId 非空）+ 一个启用的默认业务数据源。"""
    await _seedUser(dbSession, "research-user-a")
    await _seedDatasource(dbSession)
    return {"X-User-Id": "research-user-a"}


@pytest.fixture()
async def authHeadersNoSource(dbSession: AsyncSession) -> dict[str, str]:
    """用户 A 的 stub 头，但**不种任何数据源**（Task 13e「无可用源 ⇒ 显式报错」用例）。"""
    await _seedUser(dbSession, "research-user-a")
    return {"X-User-Id": "research-user-a"}


async def _seededDatasourceId(dbSession: AsyncSession) -> int:
    """`authHeaders` 夹具已种下的默认源 id（源在会话上落库，Task 13e）。"""
    return await dbSession.scalar(select(DataSource.id).order_by(DataSource.id).limit(1))


@pytest.fixture()
async def secondUserHeaders(dbSession: AsyncSession) -> dict[str, str]:
    """用户 B 的 stub 头（用于越权 404 断言）。"""
    await _seedUser(dbSession, "research-user-b")
    return {"X-User-Id": "research-user-b"}


# ---------------------------------------------------------------------------
# 后台状态机替身：空 scope 管线（确定性暂停在固定 #1）
# ---------------------------------------------------------------------------


class _EmptyEsl:
    """三臂全空：确定性触发空 scope 通道 → 固定 #1 暂停。"""

    async def extract(self, question: str, *, intent: object | None = None) -> object:
        raise EmptyResearchScopeError("三臂检索全空（测试替身）")


class _NoopPlanner:
    """无步计划：normalizePlan(None) 归一为空步计划。"""

    async def plan(self, question, client, modelName):
        return None


class _NoopRunner:
    """无步执行的替身；`adapter=` 是 Task 13e 契约（业务 SQL 必经业务库 adapter）。"""

    def __init__(self) -> None:
        self.adapters: list = []

    async def executeReadonlySql(self, session, sql, *, adapter=None):
        self.adapters.append(adapter)
        return []

    async def runVerification(self, session, hypothesis, *, adapter=None):
        return {"rows": [], "error": None}


class _EmptyModelConfigs:
    """无可用模型配置的替身（= keyless 环境形态）。

    必须注入：真实 `ModelConfigService.list` 会查 `llm_config`，而 5433 测试库
    该表缺 `disable_thinking` 列（已知基础设施漂移，Task 5 Concern 3）——
    查询失败会让注入 session 进入失败事务态，把后续 checkpoint 写入一起带崩。
    这里注入空清单，走的正是「无可用配置 → research.error 显式降级」这条真实分支。
    """

    async def list(self, session, activeOnly: bool = True) -> list:
        return []


def _fastService() -> ResearchAgentService:
    """测试用替身管线（真实 ReportPlanner 由默认分支产出，不显式注入 reporter）。"""
    return ResearchAgentService(
        esl=_EmptyEsl(),
        sessionService=ResearchSessionService(),
        planner=_NoopPlanner(),
        runner=_NoopRunner(),
        chartService=ChartService(),
        modelConfigs=_EmptyModelConfigs(),
    )


async def _createSession(
    client: AsyncClient, headers: dict[str, str], question: str = "供应商收货量为什么下降", **extra
) -> dict:
    resp = await client.post(
        f"{_BASE}/sessions", json={"question": question, **extra}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _seedRunState(
    dbSession: AsyncSession,
    *,
    sessionId: uuid.UUID,
    turnId: uuid.UUID,
    question: str = "q",
    mode: str = "research",
    datasourceId: int | None = None,
) -> ResearchRunState:
    """为手动构造 checkpoint 的测试补 run state（resumeTurn 强依赖此快照）。"""
    run_state = buildRunState(
        RunStateSeed(
            session_id=sessionId,
            turn_id=turnId,
            current_question=question,
            mode=mode,
            user_id=None,
            model_id=None,
            datasource_id=datasourceId,
        )
    )
    dbSession.add(run_state)
    await dbSession.flush()
    return run_state


async def _claimSession(
    dbSession: AsyncSession,
    *,
    sessionId: uuid.UUID,
    runStateId: uuid.UUID,
) -> None:
    """P0-X5：手动构造 session 时也要让会话持有 claim。

    真实流程中 `createTurn` 会原子地 `active_run_id = runStateId`；本测试
    用 ``appendTurn + openCheckpoint`` 跳过 createTurn，会话 ``active_run_id``
    留空。P0-X5 起 ``answerCheckpoint`` 守卫 ``claim_invariant="held"``，
    不持有 ⇒ 409，故测试也要模拟「正在跑」的状态。

    ``runStateId`` 必须是 ``research_run_state.id``（与 ``active_run_id``
    列上的 FK 对齐，不是 turn_id）。

    review M4：flush 后**不**commit，但 expire 当前 ORM 行（让后续读
    `row.active_run_id` 重新走 SQL）—— 不再依赖调用方手工 refresh。
    """
    from sqlalchemy import update

    from app.domain.research_models import ResearchSession

    await dbSession.execute(
        update(ResearchSession)
        .where(ResearchSession.id == sessionId)
        .values(active_run_id=runStateId)
    )
    await dbSession.flush()
    # review M4：让 ``sessionRow`` 在下次读 ``active_run_id`` 时重走 SQL，
    # 不依赖调用方手工 refresh。``expire`` 是 O(1) 操作。
    rowInMap = await dbSession.get(ResearchSession, sessionId)
    if rowInMap is not None:
        dbSession.expire(rowInMap, ["active_run_id"])


# ---------------------------------------------------------------------------
# 鉴权 / 越权
# ---------------------------------------------------------------------------


async def test_requires_auth(client: AsyncClient) -> None:
    """无任何身份头 → 401（研究域无 owner 语义，匿名不放行）。"""
    resp = await client.get(f"{_BASE}/sessions")
    assert resp.status_code == 401


async def test_unknown_stub_user_gets_401(client: AsyncClient) -> None:
    """stub 头指向不存在的用户（dbUserId=None）同样 401，避免 created_by=NULL 互认。"""
    resp = await client.get(f"{_BASE}/sessions", headers={"X-User-Id": "ghost-user"})
    assert resp.status_code == 401


async def test_other_user_session_404(
    client: AsyncClient, authHeaders: dict[str, str], secondUserHeaders: dict[str, str]
) -> None:
    body = await _createSession(client, authHeaders, question="q")
    sid = body["id"]

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=secondUserHeaders)
    assert detail.status_code == 404

    # 列表同样横向隔离
    listed = await client.get(f"{_BASE}/sessions", headers=secondUserHeaders)
    assert listed.status_code == 200
    assert listed.json() == []

    # 报告 / 轮次子资源也 404（不泄露存在性）
    for suffix in ("report", "reports"):
        resp = await client.get(f"{_BASE}/sessions/{sid}/{suffix}", headers=secondUserHeaders)
        assert resp.status_code == 404


async def test_other_user_cannot_post_turn(
    client: AsyncClient, authHeaders: dict[str, str], secondUserHeaders: dict[str, str]
) -> None:
    body = await _createSession(client, authHeaders, question="q")
    resp = await client.post(
        f"{_BASE}/sessions/{body['id']}/turns",
        json={"question": "越权"},
        headers=secondUserHeaders,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 会话读写
# ---------------------------------------------------------------------------


async def test_create_and_get_session(client: AsyncClient, authHeaders: dict[str, str]) -> None:
    body = await _createSession(client, authHeaders)
    assert body["status"] == "running"
    assert body["mode"] == "research"
    assert body["question"] == "供应商收货量为什么下降"

    sid = body["id"]
    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["session"]["id"] == sid
    assert payload["turns"] == []
    assert payload["pendingCheckpoint"] is None

    listed = await client.get(f"{_BASE}/sessions", headers=authHeaders)
    assert [row["id"] for row in listed.json()] == [sid]


async def test_invalid_mode_is_422(client: AsyncClient, authHeaders: dict[str, str]) -> None:
    """非法 mode 在 Pydantic 边界 422（不再拖到报告阶段炸）。"""
    resp = await client.post(
        f"{_BASE}/sessions",
        json={"question": "q", "mode": "bogus"},
        headers=authHeaders,
    )
    assert resp.status_code == 422


async def test_extra_field_is_422(client: AsyncClient, authHeaders: dict[str, str]) -> None:
    """extra=forbid：未声明字段被拒。"""
    resp = await client.post(
        f"{_BASE}/sessions",
        json={"question": "q", "nope": 1},
        headers=authHeaders,
    )
    assert resp.status_code == 422


async def test_pending_checkpoint_prompt_derived_from_options(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """pendingCheckpoint.prompt 由 options["prompt"] 派生（无独立列）。"""
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    row = await svc.createSession(
        dbSession, userId=userId, question="q", datasourceId=await _seededDatasourceId(dbSession)
    )
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
    await svc.openCheckpoint(
        dbSession,
        sessionId=row.id,
        turnId=turn.id,
        phase="intent",
        options={"signal": "fixed_scope"},
        prompt="三臂是否齐全？",
    )
    await dbSession.commit()

    resp = await client.get(f"{_BASE}/sessions/{row.id}", headers=authHeaders)
    assert resp.status_code == 200
    cp = resp.json()["pendingCheckpoint"]
    assert cp["prompt"] == "三臂是否齐全？"
    assert cp["options"]["prompt"] == "三臂是否齐全？"
    assert cp["phase"] == "intent" and cp["status"] == "pending"


async def test_turn_returns_202_and_state_machine_runs(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST turns → 202 立即返回；状态机在后台跑完（暂停在固定 #1）。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fastService)
    created = await _createSession(client, authHeaders)
    sid = created["id"]

    resp = await client.post(
        f"{_BASE}/sessions/{sid}/turns",
        json={"question": "供应商收货量为什么下降"},
        headers=authHeaders,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["sessionId"] == sid
    assert body["status"] == "running"
    assert uuid.UUID(body["turnId"])

    # BackgroundTasks 在 ASGI 调用内完成 ⇒ 此处读到的已是状态机跑完后的终态
    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    payload = detail.json()
    assert payload["session"]["status"] == "awaiting_user"
    assert payload["pendingCheckpoint"]["phase"] == "intent"
    assert payload["pendingCheckpoint"]["prompt"]
    assert [t["role"] for t in payload["turns"]] == ["user", "checkpoint_awaiting"]


async def test_turn_with_whitespace_question_marks_session_failed(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 8.5：空白问题在 `_guardedRun` 之前抛 ⇒ 后台 wrapper 落终态 failed。

    Pydantic `min_length=1` 只挡空串，`"   "` 能过边界；守卫下沉到 `runTurn` 后抛
    ValueError，逃到 `_runTurnInBackground` 的 except。旧实现只补发 error 事件、不改
    会话状态（停在 running），现在按 ERROR_SPECS 把会话落 failed（表里写的
    `turn_failed → sessionStatus=failed` 在真实数据上成立）。状态机本身仍不推进：
    无 pending checkpoint、无 checkpoint_awaiting 轮次。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fastService)
    created = await _createSession(client, authHeaders, question="q")

    resp = await client.post(
        f"{_BASE}/sessions/{created['id']}/turns",
        json={"question": "   "},
        headers=authHeaders,
    )
    assert resp.status_code == 202

    detail = await client.get(f"{_BASE}/sessions/{created['id']}", headers=authHeaders)
    payload = detail.json()
    assert payload["pendingCheckpoint"] is None
    assert [t["role"] for t in payload["turns"]] == ["user"]  # 只有请求事务里写的 user turn
    assert payload["session"]["status"] == "failed"


async def test_checkpoint_answer_replay_is_idempotent_but_conflicting_decision_409(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession, monkeypatch
) -> None:
    """Task 2 契约变更：**完全相同**的重复提交 ⇒ 幂等回放 200；**不同决策** ⇒ 409。

    Task 1 及以前这里是「第二次提交一律 409」，把双击/超时重试当成冲突顶回去。
    Task 2 要求决策幂等（`answerCheckpoint` 按已落库的 `(status→action, user_choice)`
    比对）：同 action + 同 choice 返回与原提交**相同**的响应体；只有**换了决策**
    才算真冲突。故 409 的断言改为「换一个 action」，并对 409 断言**是谁拒的**文案
    （只断状态码会让守卫前移或误判也全绿）。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fastService)
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    datasourceId = await _seededDatasourceId(dbSession)
    row = await svc.createSession(
        dbSession, userId=userId, question="q", datasourceId=datasourceId
    )
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
    runState = await _seedRunState(
        dbSession, sessionId=row.id, turnId=turn.id, datasourceId=datasourceId
    )
    await _claimSession(dbSession, sessionId=row.id, runStateId=runState.id)
    cp = await svc.openCheckpoint(
        dbSession,
        sessionId=row.id,
        turnId=turn.id,
        phase="intent",
        options={"signal": "fixed_scope", "resumePhase": "plan"},
        prompt="三臂是否齐全？",
    )
    await dbSession.commit()

    resp = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "confirm"},
        headers=authHeaders,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sessionStatus": "running", "nextPhase": "plan"}

    # 同一 (action, choice) 重放（双击 / 超时重试）：幂等 —— 200 且**决策不被重新应用**
    # （同一 nextPhase）。`sessionStatus` 不逐字相同是**刻意**的：它是当前会话状态快照，
    # 回放时机器已在下一点暂停故如实报 awaiting_user。
    again = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "confirm"},
        headers=authHeaders,
    )
    assert again.status_code == 200, again.text
    assert again.json()["nextPhase"] == resp.json()["nextPhase"]
    detail = await client.get(f"{_BASE}/sessions/{row.id}", headers=authHeaders)
    assert again.json()["sessionStatus"] == detail.json()["session"]["status"]

    # 换了决策（confirm → reject）才算真冲突：409 且文案指明「已决策」
    conflict = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "reject"},
        headers=authHeaders,
    )
    assert conflict.status_code == 409, conflict.text
    assert "已决策" in conflict.json()["error"]


async def test_checkpoint_answer_at_planning_modify_reports_replan_phase(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession, monkeypatch
) -> None:
    """planning 点 modify ⇒ 机器重新暂停在 **plan**，响应不得报 execute（M3）。

    端点同步算这个值（后台任务之前），所以响应体与本断言不受后台执行影响；被判别的
    是「端点用的判定」而不是「机器实际跑成什么」，后者由
    test_planning_modify_reruns_planner_with_choice 覆盖。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fastService)
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    datasourceId = await _seededDatasourceId(dbSession)
    row = await svc.createSession(
        dbSession, userId=userId, question="q", datasourceId=datasourceId
    )
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
    runState = await _seedRunState(
        dbSession, sessionId=row.id, turnId=turn.id, datasourceId=datasourceId
    )
    await _claimSession(dbSession, sessionId=row.id, runStateId=runState.id)
    cp = await svc.openCheckpoint(
        dbSession,
        sessionId=row.id,
        turnId=turn.id,
        phase="planning",
        options={"signal": "fixed_plan", "resumePhase": "execute", OPT_REPLAN_COUNT: 0},
        prompt="计划是否确认？",
    )
    await dbSession.commit()

    resp = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "modify", "choice": {"instruction": "只看华东"}},
        headers=authHeaders,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sessionStatus": "running", "nextPhase": "plan"}


async def test_checkpoint_answer_invalid_action_422(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    row = await svc.createSession(
        dbSession, userId=userId, question="q", datasourceId=await _seededDatasourceId(dbSession)
    )
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
    runState = await _seedRunState(
        dbSession, sessionId=row.id, turnId=turn.id,
        datasourceId=await _seededDatasourceId(dbSession),
    )
    await _claimSession(dbSession, sessionId=row.id, runStateId=runState.id)
    cp = await svc.openCheckpoint(
        dbSession, sessionId=row.id, turnId=turn.id, phase="intent",
        options={"resumePhase": "plan"}, prompt="p",
    )
    await dbSession.commit()

    resp = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "nope"},
        headers=authHeaders,
    )
    assert resp.status_code == 422


async def test_answer_request_rejects_extra_field() -> None:
    """DTO 边界：checkpoint answer 请求体 extra=forbid。"""
    with pytest.raises(ValidationError):
        CheckpointAnswerRequest.model_validate({"action": "confirm", "bogus": 1})


async def test_answer_checkpoint_409_when_session_has_no_claim(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """P0-X5：``answerCheckpoint`` 守卫 ``claim_invariant="held"``。

    真实流程下，会话在 ``running`` 状态机停检查点时仍持有 ``active_run_id``；
    但 abort 之后会清空 claim —— 此时若再答 pending checkpoint（旧实现
    漏的洞），``resumeTurn`` 会把 ``aborted`` 拉回 ``running`` 而不 claim，
    出现 ``running + active_run_id=NULL`` 的会话，下次 POST /turns 放行、
    起第二条状态机。

    本测试构造「会话存在 + 有 pending checkpoint + 无 claim」场景，断言
    端点 409 + 文案指明「未持有执行权」。
    """
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    row = await svc.createSession(
        dbSession, userId=userId, question="q", datasourceId=await _seededDatasourceId(dbSession)
    )
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
    cp = await svc.openCheckpoint(
        dbSession, sessionId=row.id, turnId=turn.id, phase="intent",
        options={"resumePhase": "plan"}, prompt="p",
    )
    await dbSession.commit()
    # 关键：**不**调 ``_claimSession`` —— 模拟 abort 后的状态。

    resp = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "confirm"},
        headers=authHeaders,
    )
    assert resp.status_code == 409, resp.text
    assert "未持有执行权" in resp.json()["error"]
    assert "active_run_id 为空" in resp.json()["error"]


async def test_abort_then_answer_pending_checkpoint_409(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """P0-X5 review M3：端到端 abort→answer race 端到端覆盖。

    旧实现下：abort 把会话落 ``aborted`` + 清空 ``active_run_id``，但
    pending checkpoint 仍 ``status="pending"``。再答 checkpoint，
    ``resumeTurn`` 把 ``aborted`` 拉回 ``running``（且不 claim），出现
    ``running + active_run_id=NULL`` 的会话，下次 POST /turns 放行、
    起第二条状态机。详见记忆 [[qa-system-research-claim-invariant-create-only]]。

    本测试**不**手工跳过 ``_claimSession``，而是真走 ``POST /abort`` 把
    claim 释放掉，然后断言 ``POST /checkpoints/{id}/answer`` 409。
    守护 ``abortSessionIfActive`` 的释放语义（一旦该 SQL 漏写
    ``active_run_id=None``，本测试立刻 RED）。
    """
    from sqlalchemy import select

    from app.domain.research_models import ResearchSession

    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    row = await svc.createSession(
        dbSession, userId=userId, question="q", datasourceId=await _seededDatasourceId(dbSession)
    )
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
    runState = await _seedRunState(
        dbSession, sessionId=row.id, turnId=turn.id,
        datasourceId=await _seededDatasourceId(dbSession),
    )
    await _claimSession(dbSession, sessionId=row.id, runStateId=runState.id)
    cp = await svc.openCheckpoint(
        dbSession, sessionId=row.id, turnId=turn.id, phase="intent",
        options={"resumePhase": "plan"}, prompt="p",
    )
    # 把会话推成 ``awaiting_user``（abort 白名单是 awaiting_user）：
    row.status = "awaiting_user"
    await dbSession.commit()

    # 真走 abort 端点：白名单 awaiting_user ⇒ 200 + cancelled + 释放 claim
    abortResp = await client.post(
        f"{_BASE}/sessions/{row.id}/abort", headers=authHeaders
    )
    assert abortResp.status_code == 200, abortResp.text
    assert abortResp.json()["cancelled"] is True
    assert abortResp.json()["sessionStatus"] == "aborted"

    # 守护性断言：abort 之后库中 ``active_run_id`` 真为 NULL（不是 stale）。
    # 若有人重构 ``abortSessionIfActive`` 漏写 ``active_run_id=None``，
    # 本 SELECT 立刻 RED —— 也就是说，下面的 409 测试不再是「写出来」的。
    await dbSession.refresh(row)
    assert row.active_run_id is None
    assert row.status == "aborted"

    # 现在答 pending checkpoint：必须 409，不能让 resumeTurn 把它拉回 running
    answerResp = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "confirm"},
        headers=authHeaders,
    )
    assert answerResp.status_code == 409, answerResp.text
    assert "未持有执行权" in answerResp.json()["error"]

    # 守护：会话**仍然**是 ``aborted`` + 无 claim —— 不被续跑污染
    await dbSession.refresh(row)
    assert row.status == "aborted"
    assert row.active_run_id is None


# ---------------------------------------------------------------------------
# 报告版本端点
# ---------------------------------------------------------------------------


async def test_report_version_endpoints(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    row = await svc.createSession(
        dbSession, userId=userId, question="q", datasourceId=await _seededDatasourceId(dbSession)
    )
    await svc.publishReport(dbSession, sessionId=row.id, payload={"v": 1}, renderedMd="# v1")
    await svc.publishReport(dbSession, sessionId=row.id, payload={"v": 2}, renderedMd="# v2")
    await dbSession.commit()

    current = await client.get(f"{_BASE}/sessions/{row.id}/report", headers=authHeaders)
    assert current.status_code == 200
    assert current.json()["version"] == 2
    assert current.json()["status"] == "published"
    assert current.json()["payload"] == {"v": 2}
    assert current.json()["renderedMd"] == "# v2"

    first = await client.get(f"{_BASE}/sessions/{row.id}/report?version=1", headers=authHeaders)
    assert first.status_code == 200
    assert first.json()["version"] == 1 and first.json()["status"] == "superseded"

    versions = await client.get(f"{_BASE}/sessions/{row.id}/reports", headers=authHeaders)
    assert [r["version"] for r in versions.json()] == [1, 2]

    missing = await client.get(f"{_BASE}/sessions/{row.id}/report?version=9", headers=authHeaders)
    assert missing.status_code == 404


async def test_report_missing_when_never_published(
    client: AsyncClient, authHeaders: dict[str, str]
) -> None:
    created = await _createSession(client, authHeaders, question="q")
    resp = await client.get(f"{_BASE}/sessions/{created['id']}/report", headers=authHeaders)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 默认构造点（Task 6 F1 / Task 6.5 M3）：API 是首个生产构造点
# ---------------------------------------------------------------------------


async def test_default_construction_uses_real_reporter_and_llm_factory() -> None:
    """不传 reporter ⇒ 默认分支产出真实 ReportPlanner；llmFactory = 真 createClient。

    keyless 环境由状态机的 `research.error` 降级路径兜底（不在此处断言 LLM 可用）。

    为什么断言**完整依赖集合**：本测试是生产构造点 `buildResearchAgentService()`
    的**唯一守卫**——端点测试把它 monkeypatch 成假替身，从外面看不见工厂内部。
    只挑手选子集会漏掉漏注的依赖：`nl2sql` 曾在生产构造点被漏注（该能力静默关闭，
    计划步全部 STEP_MISSING_SQL、数据事件从不触发），而 4 条旧断言恰好不含
    `_nl2sql`，P1 因此漏网。故此处补齐 `_nl2sql` 断言，与既有的 reporter / llmFactory /
    esl / sessionService 一起守住全集。
    """
    service = researchModule.buildResearchAgentService()
    assert isinstance(service._reporter, ReportPlanner)
    assert service._llmFactory is createClient
    assert isinstance(service._esl, EnterpriseSemanticLayer)
    assert isinstance(service.sessionService, ResearchSessionService)
    assert isinstance(service._nl2sql, Nl2SqlService)


async def test_turn_endpoint_uses_default_construction_path(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """端点确实经 `buildResearchAgentService()` 构造（spy 包一层再委派替身）。"""
    seen: list[ResearchAgentService] = []

    def _spy() -> ResearchAgentService:
        service = _fastService()
        seen.append(service)
        return service

    monkeypatch.setattr(researchModule, "buildResearchAgentService", _spy)
    created = await _createSession(client, authHeaders, question="q")
    resp = await client.post(
        f"{_BASE}/sessions/{created['id']}/turns",
        json={"question": "q"},
        headers=authHeaders,
    )
    assert resp.status_code == 202
    assert len(seen) == 1
    assert isinstance(seen[0]._reporter, ReportPlanner)


# ---------------------------------------------------------------------------
# Task 13e：业务数据源解析与持久化（研究侧终于连上业务库）
# ---------------------------------------------------------------------------


async def _persistedDatasourceId(dbSession: AsyncSession, sessionId: str):
    return await dbSession.scalar(
        select(ResearchSession.datasource_id).where(ResearchSession.id == uuid.UUID(sessionId))
    )


async def test_create_session_persists_default_datasource(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """缺省建会话 → 绑定**默认业务源**并把 id 落库。

    源必须持久化（不是只活在请求内存里）：研究是多轮 + 可恢复的，`state` 不落库，
    后续 turn 的执行相位只能从 `research_session.datasource_id` 找回同一个库。
    """
    created = await _createSession(client, authHeaders)
    assert await _persistedDatasourceId(dbSession, created["id"]) == await _seededDatasourceId(
        dbSession
    )


async def test_create_session_honors_explicit_datasource(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """显式 `datasourceId` 优先于默认源（字段可选 ⇒ 前端零改动）。"""
    other = await _seedDatasource(dbSession, "api-test-secondary", isDefault=False)
    created = await _createSession(client, authHeaders, datasourceId=other.id)
    assert await _persistedDatasourceId(dbSession, created["id"]) == other.id


async def test_create_session_without_active_datasource_is_rejected(
    client: AsyncClient, authHeadersNoSource: dict[str, str]
) -> None:
    """无可用业务源 ⇒ **显式报错**，绝不静默回落到应用元数据库会话。

    回落正是本特性此前的根因（业务 SQL 打在 Postgres 元数据库上 ⇒ 一律
    `UndefinedTableError`），所以这里断言的是「建会话就失败」，而不是建完再炸。
    """
    resp = await client.post(
        f"{_BASE}/sessions", json={"question": "q"}, headers=authHeadersNoSource
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == MSG_DATASOURCE_NONE_AVAILABLE


async def test_create_session_with_unknown_datasource_is_404(
    client: AsyncClient, authHeaders: dict[str, str]
) -> None:
    """显式 id 不存在 ⇒ 404（`DataSourceService.get` 的 NotFoundError），不静默改用默认源。"""
    resp = await client.post(
        f"{_BASE}/sessions", json={"question": "q", "datasourceId": 999999}, headers=authHeaders
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# W3：删除会话（硬删 + 级联 + 归属 404）
# ---------------------------------------------------------------------------


async def _countRows(dbSession: AsyncSession, model: Any, sessionId: uuid.UUID) -> int:
    """统计某会话在子表中的行数（删后应全为 0 —— 验证 CASCADE 真的生效）。"""
    return await dbSession.scalar(
        select(func.count()).select_from(model).where(model.session_id == sessionId)
    )


async def test_delete_session_cascades_children_and_404_afterwards(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """删除会话 → 204；turn / checkpoint / report 子行一并消失；再读 404。"""
    created = await _createSession(client, authHeaders, question="q")
    sid = uuid.UUID(created["id"])
    svc = ResearchSessionService()
    turn = await svc.appendTurn(dbSession, sessionId=sid, role="user", content={})
    runState = await _seedRunState(
        dbSession, sessionId=sid, turnId=turn.id, datasourceId=None,
    )
    await _claimSession(dbSession, sessionId=sid, runStateId=runState.id)
    await svc.openCheckpoint(
        dbSession, sessionId=sid, turnId=turn.id, phase="intent", options={}, prompt="p"
    )
    await svc.publishReport(dbSession, sessionId=sid, payload={"title": "t"}, renderedMd="# t")
    await dbSession.commit()

    resp = await client.delete(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.status_code == 404
    assert await _countRows(dbSession, ResearchTurn, sid) == 0
    assert await _countRows(dbSession, ResearchCheckpoint, sid) == 0
    assert await _countRows(dbSession, ResearchReport, sid) == 0


async def test_delete_other_user_session_404_and_kept(
    client: AsyncClient,
    authHeaders: dict[str, str],
    secondUserHeaders: dict[str, str],
) -> None:
    """他人会话删除 → 404，且**真的没删**（不泄露存在性，也不做横向越权写）。"""
    created = await _createSession(client, authHeaders, question="q")
    resp = await client.delete(f"{_BASE}/sessions/{created['id']}", headers=secondUserHeaders)
    assert resp.status_code == 404

    kept = await client.get(f"{_BASE}/sessions/{created['id']}", headers=authHeaders)
    assert kept.status_code == 200


async def test_delete_missing_session_404(
    client: AsyncClient, authHeaders: dict[str, str]
) -> None:
    resp = await client.delete(f"{_BASE}/sessions/{uuid.uuid4()}", headers=authHeaders)
    assert resp.status_code == 404


async def test_delete_requires_auth(client: AsyncClient) -> None:
    """匿名删除 → 401（未过鉴权，不泄露会话是否存在）。"""
    resp = await client.delete(f"{_BASE}/sessions/{uuid.uuid4()}")
    assert resp.status_code == 401


async def test_session_read_echoes_datasource_id(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """响应回显 datasourceId（W4）：创建响应与列表响应都要有（治「不知道对哪个库研究」）。"""
    expected = await _seededDatasourceId(dbSession)
    created = await _createSession(client, authHeaders, question="q")
    assert created["datasourceId"] == expected

    listed = await client.get(f"{_BASE}/sessions", headers=authHeaders)
    assert listed.status_code == 200
    assert listed.json()[0]["datasourceId"] == expected


# ---------------------------------------------------------------------------
# W5：模型选择（会话级落库 + 显式校验）
# ---------------------------------------------------------------------------


async def _seedModel(
    dbSession: AsyncSession, name: str = "api-test-model", *, isActive: bool = True
) -> LlmConfig:
    """种一个模型配置（api_endpoint 指向不可达端口：本文件不真的调 LLM）。"""
    row = LlmConfig(
        model_name=name,
        provider="openai",
        api_endpoint="http://127.0.0.1:9/v1",
        api_key_encrypted=encryptApiKey("test-key"),
        is_active=isActive,
    )
    dbSession.add(row)
    await dbSession.commit()
    await dbSession.refresh(row)
    return row


async def test_create_session_honors_explicit_model(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    model = await _seedModel(dbSession)
    created = await _createSession(client, authHeaders, question="q", modelId=model.id)

    assert created["modelId"] == model.id
    row = await dbSession.get(ResearchSession, uuid.UUID(created["id"]))
    assert row.model_id == model.id


async def test_create_session_defaults_to_auto_routing(
    client: AsyncClient, authHeaders: dict[str, str]
) -> None:
    """不传 modelId ⇒ None（自动路由行为与今天完全一致）。"""
    created = await _createSession(client, authHeaders, question="q")
    assert created["modelId"] is None


async def test_create_session_with_unknown_model_404(
    client: AsyncClient, authHeaders: dict[str, str]
) -> None:
    resp = await client.post(
        f"{_BASE}/sessions", json={"question": "q", "modelId": 999999}, headers=authHeaders
    )
    assert resp.status_code == 404, resp.text
    assert "999999" in resp.text


async def test_create_session_with_inactive_model_404(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """已停用的模型：报错而非静默换模型（否则用户以为用了 A 实际用了 B）。"""
    model = await _seedModel(dbSession, "api-test-inactive-model", isActive=False)
    resp = await client.post(
        f"{_BASE}/sessions", json={"question": "q", "modelId": model.id}, headers=authHeaders
    )
    assert resp.status_code == 404, resp.text
