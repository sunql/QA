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

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.research_schemas import CheckpointAnswerRequest
from app.infrastructure.llm.factory import createClient
from app.models.rbac import User
from app.services.chart_service import ChartService
from app.services.enterprise_semantic_layer import (
    EmptyResearchScopeError,
    EnterpriseSemanticLayer,
)
from app.services.report_planner import ReportPlanner
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


@pytest.fixture()
async def authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    """用户 A 的 stub 头（DB 命中 → dbUserId 非空）。"""
    await _seedUser(dbSession, "research-user-a")
    return {"X-User-Id": "research-user-a"}


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

    async def plan(self, question, classes, client, modelName):
        return None


class _NoopRunner:
    async def executeReadonlySql(self, session, sql):
        return []

    async def runVerification(self, session, hypothesis):
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
    row = await svc.createSession(dbSession, userId=userId, question="q")
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


async def test_turn_with_whitespace_question_does_not_advance(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOW：空白问题经 API 路径（createTurn → appendTurn → runTurn）被守卫拦下。

    Pydantic `min_length=1` 只挡空串，`"   "` 能过边界；守卫下沉到 `runTurn` 后，
    后台状态机在写 checkpoint 之前就 ValueError ⇒ 会话不推进（无 pending 检查点、
    无 checkpoint_awaiting 轮次）。
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
    assert payload["session"]["status"] == "running"


async def test_checkpoint_answer_and_double_submit_409(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fastService)
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    row = await svc.createSession(dbSession, userId=userId, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
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

    # 决策已落库 + 状态机后台续跑到固定 #2
    again = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "confirm"},
        headers=authHeaders,
    )
    assert again.status_code == 409


async def test_checkpoint_answer_invalid_action_422(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    row = await svc.createSession(dbSession, userId=userId, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
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


# ---------------------------------------------------------------------------
# 报告版本端点
# ---------------------------------------------------------------------------


async def test_report_version_endpoints(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    row = await svc.createSession(dbSession, userId=userId, question="q")
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
    """
    service = researchModule.buildResearchAgentService()
    assert isinstance(service._reporter, ReportPlanner)
    assert service._llmFactory is createClient
    assert isinstance(service._esl, EnterpriseSemanticLayer)
    assert isinstance(service.sessionService, ResearchSessionService)


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
