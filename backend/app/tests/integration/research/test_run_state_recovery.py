"""ResearchRunState 恢复态契约：改写/追问后的问题不回落到 input_seed。

真实 PG + 完整 HTTP 链路；状态机注入确定性 fake 管线（零网络 / 零 LLM），
只验证 runState 的创建、读取与恢复态重建路径。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource, User
from app.domain.research_models import ResearchRunState
from app.infrastructure.security.crypto import encryptApiKey
from app.services.chart_service import ChartService
from app.services.enterprise_semantic_layer import (
    BusinessObjectRef,
    ESLExtraction,
)
from app.services.research_agent_service import ResearchAgentService
from app.services.research_session_service import ResearchSessionService
from app.services.step_query_planner import StepPlanResult

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/research"


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
        name=f"run-state-test-{uuid.uuid4().hex[:8]}",
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
    await _seedUser(dbSession, "run-state-user")
    await _seedDatasource(dbSession)
    return {"X-User-Id": "run-state-user"}


async def _activeDatasourceId(dbSession: AsyncSession) -> int:
    return await dbSession.scalar(select(DataSource.id).where(DataSource.is_active.is_(True)).limit(1))


class _FixedEsl:
    """确定性三臂：命中 1 个业务对象、0 冲突，让状态机走到范围确认点。"""

    def __init__(self, seen: list[str] | None = None) -> None:
        self.seen: list[str] = seen if seen is not None else []

    async def extract(self, question: str, *, intent: object | None = None) -> ESLExtraction:
        self.seen.append(question)
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


class _SingleStepPlanner:
    """返回两步计划（1 数据步 + 1 汇总步），让状态机暂停在计划确认点。"""

    def __init__(self, seen: list[str] | None = None) -> None:
        self.seen: list[str] = seen if seen is not None else []

    async def plan(self, question: str, client: Any, modelName: str | None) -> StepPlanResult:
        self.seen.append(question)
        return StepPlanResult(
            plan=None,
            prompt_tokens=0,
            completion_tokens=0,
        )


class _NoopRunner:
    """不执行业务 SQL；execute 阶段因 step 缺 SQL 进入 low_confidence 动态点。"""

    async def executeReadonlySql(self, session: AsyncSession, sql: str, *, adapter: Any = None) -> list[dict[str, Any]]:
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


def _runStateService() -> ResearchAgentService:
    """走 fake 管线，但用真实 ResearchSessionService。"""
    return ResearchAgentService(
        esl=_FixedEsl(),
        sessionService=ResearchSessionService(),
        planner=_SingleStepPlanner(),
        runner=_NoopRunner(),
        chartService=ChartService(),
        modelConfigs=_EmptyModelConfigs(),
        nl2sql=None,
    )


async def _createSession(client: AsyncClient, headers: dict[str, str], question: str) -> dict[str, Any]:
    resp = await client.post(f"{_BASE}/sessions", json={"question": question}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _waitForCheckpoint(
    client: AsyncClient,
    headers: dict[str, str],
    sessionId: str,
    phase: str,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """轮询会话详情，直到出现目标 phase 的 pending checkpoint。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        resp = await client.get(f"{_BASE}/sessions/{sessionId}", headers=headers)
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        cp = payload.get("pendingCheckpoint")
        if cp and cp["phase"] == phase and cp["status"] == "pending":
            return cp
        await asyncio.sleep(0.05)
    raise RuntimeError(f"未在 {timeout}s 内等到 {phase} checkpoint")


async def test_resume_turn_keeps_current_question(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """追问的问题写入 ResearchRunState；恢复态取 runState.current_question 而非 input_seed。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _runStateService)

    seed_question = "分析2025年采购总额"
    turn_question = "只分析2024年订单数量"

    session = await _createSession(client, authHeaders, question=seed_question)
    sid = session["id"]

    r1 = await client.post(
        f"{_BASE}/sessions/{sid}/turns",
        json={"question": turn_question},
        headers=authHeaders,
    )
    assert r1.status_code == 202, r1.text
    turn1 = r1.json()["turnId"]

    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")
    r2 = await client.post(
        f"{_BASE}/checkpoints/{cp['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert r2.status_code == 200, r2.text

    cp_plan = await _waitForCheckpoint(client, authHeaders, sid, phase="planning")
    r3 = await client.post(
        f"{_BASE}/checkpoints/{cp_plan['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert r3.status_code == 200, r3.text

    run_state = await dbSession.scalar(
        select(ResearchRunState)
        .where(ResearchRunState.session_id == uuid.UUID(sid))
        .where(ResearchRunState.turn_id == uuid.UUID(turn1))
    )
    assert run_state is not None, "ResearchRunState 未创建"
    assert run_state.current_question == turn_question
    assert run_state.current_question != seed_question


async def test_rewritten_question_persisted_and_restored_across_resume(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """计划点改写问题：新问题落 ResearchRunState（scope_version+1），

    后续 resume 的重建态从 runState.current_question 取问题（而非 input_seed）。
    """
    esl_seen: list[str] = []
    plan_seen: list[str] = []
    svc = ResearchAgentService(
        esl=_FixedEsl(seen=esl_seen),
        sessionService=ResearchSessionService(),
        planner=_SingleStepPlanner(seen=plan_seen),
        runner=_NoopRunner(),
        chartService=ChartService(),
        modelConfigs=_EmptyModelConfigs(),
        nl2sql=None,
    )
    monkeypatch.setattr(researchModule, "buildResearchAgentService", lambda: svc)

    seed_question = "分析2025年采购总额"
    first_question = "只分析2024年订单数量"
    rewritten_question = "改成2023年供应商交付准时率"

    session = await _createSession(client, authHeaders, question=seed_question)
    sid = session["id"]

    r1 = await client.post(
        f"{_BASE}/sessions/{sid}/turns",
        json={"question": first_question},
        headers=authHeaders,
    )
    assert r1.status_code == 202, r1.text
    turn1 = r1.json()["turnId"]

    cp = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")
    r2 = await client.post(
        f"{_BASE}/checkpoints/{cp['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert r2.status_code == 200, r2.text

    cp_plan = await _waitForCheckpoint(client, authHeaders, sid, phase="planning")
    # Act: 计划点改写问题（choice.question 通道）
    r3 = await client.post(
        f"{_BASE}/checkpoints/{cp_plan['id']}/answer",
        json={"action": "modify", "choice": {"question": rewritten_question}},
        headers=authHeaders,
    )
    assert r3.status_code == 200, r3.text

    # 改写 ⇒ 回 ESL 相位重跑，ESL 收到的问题必须是改写后的
    cp_intent2 = await _waitForCheckpoint(client, authHeaders, sid, phase="intent")
    assert esl_seen, "ESL 未被调用"
    assert esl_seen[-1] == rewritten_question

    # 断言 1：改写后的问题已持久化，且 scope_version 递增
    db_state = await dbSession.scalar(
        select(ResearchRunState)
        .where(ResearchRunState.turn_id == uuid.UUID(turn1))
        .order_by(ResearchRunState.created_at.desc(), ResearchRunState.id.desc())
    )
    assert db_state is not None, "改写后未写入 ResearchRunState 版本"
    assert db_state.current_question == rewritten_question
    assert db_state.current_question != seed_question
    assert db_state.current_question != first_question
    assert db_state.scope_version == 2

    # 断言 2：确认改写后的范围再 resume，planner 收到的问题来自 runState.current_question
    r4 = await client.post(
        f"{_BASE}/checkpoints/{cp_intent2['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert r4.status_code == 200, r4.text
    await _waitForCheckpoint(client, authHeaders, sid, phase="planning")
    assert plan_seen, "planner 未被调用"
    # Task 13：planner 输入的**末段**是 mode 指引（本会话为默认 mode），故按「以改写后的
    # 问题开头」断言 —— 本用例要证的仍是「问题来自 runState.current_question」而非 seed。
    assert plan_seen[-1].startswith(rewritten_question), plan_seen[-1]
    assert not plan_seen[-1].startswith(seed_question), plan_seen[-1]
