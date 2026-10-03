"""research_session_service 持久化链路（真实 PostgreSQL，串行）。

Task 3 契约测试：会话 / 轮次 / checkpoint / finding / report 五张表的写入链路。
ORM 列属性为 snake_case（见 app/domain/research_models.py），故断言用 snake_case。
"""

import uuid

import pytest
from sqlalchemy import text

from app.domain.research_models import ResearchSession
from app.services.research_session_service import ResearchSessionService


@pytest.mark.asyncio
async def test_create_session_and_first_turn(dbSession) -> None:
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="供应商收货量为什么下降")
    assert s.status == "running" and s.mode == "research" and s.input_seed
    turn = await svc.appendTurn(
        dbSession, sessionId=s.id, role="user", content={"question": "供应商收货量为什么下降"}
    )
    assert turn.turn_index == 0 and turn.role == "user"


@pytest.mark.asyncio
async def test_checkpoint_open_resolve_and_pending_lookup(dbSession) -> None:
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})
    cp = await svc.openCheckpoint(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        phase="intent",
        options={"arms": ["bo", "metric"]},
        prompt="三臂是否齐全？",
    )
    assert cp.status == "pending"
    assert cp.options["prompt"] == "三臂是否齐全？"  # prompt 无独立列，随 options 落 JSONB
    pending = await svc.getPendingCheckpoint(dbSession, s.id)
    assert pending is not None and pending.id == cp.id
    resolved = await svc.resolveCheckpoint(
        dbSession, checkpointId=cp.id, status="confirmed", userChoice={"action": "confirm"}
    )
    assert resolved.status == "confirmed" and resolved.decided_at is not None
    assert await svc.getPendingCheckpoint(dbSession, s.id) is None
    with pytest.raises(ValueError):
        await svc.resolveCheckpoint(
            dbSession, checkpointId=cp.id, status="confirmed", userChoice={}
        )
    # 白名单：把已决策的 checkpoint 重新置回 pending 会让 getPendingCheckpoint
    # 再次返回它（重复决策洞），必须拒绝。
    with pytest.raises(ValueError):
        await svc.resolveCheckpoint(
            dbSession, checkpointId=cp.id, status="pending", userChoice={}
        )


@pytest.mark.asyncio
async def test_publish_report_versioning(dbSession) -> None:
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="q")
    r1 = await svc.publishReport(dbSession, sessionId=s.id, payload={"v": 1}, renderedMd="# v1")
    r2 = await svc.publishReport(dbSession, sessionId=s.id, payload={"v": 2}, renderedMd="# v2")
    assert (r1.version, r2.version) == (1, 2)
    assert r1.status == "superseded" and r2.status == "published"
    reports = await svc.listReports(dbSession, s.id)
    assert {r.status for r in reports} == {"superseded", "published"}
    # DB 真值断言：绕过 identity map，证明 supersede 落到了库里（否则旧 published
    # 仍在，部分唯一索引会在下一次 INSERT 时炸；仅断言内存属性会假绿）。
    rows = (
        await dbSession.execute(
            text(
                "SELECT version, status FROM research_report "
                "WHERE session_id = :sid ORDER BY version"
            ),
            {"sid": s.id},
        )
    ).all()
    assert [(r[0], r[1]) for r in rows] == [(1, "superseded"), (2, "published")]


@pytest.mark.asyncio
async def test_save_finding_and_update_session_status(dbSession) -> None:
    """补齐 finding / session 状态 / 未知 checkpoint 三条契约。"""
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=None, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})
    finding = await svc.saveFinding(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        claimText="华东收货量下降 12%",
        supportingSql="SELECT 1",
        supportingData={"rows": 1},
        confidence=0.82,
    )
    assert finding.claim_text == "华东收货量下降 12%"
    assert finding.turn_id == turn.id and finding.confidence is not None

    await svc.updateSessionStatus(dbSession, s.id, "done")
    reloaded = await dbSession.get(ResearchSession, s.id)
    assert reloaded is not None and reloaded.status == "done"
    # DB 真值断言（绕过 identity map）：finding 与状态确实落库，非仅内存态。
    dbFinding = (
        await dbSession.execute(
            text("SELECT claim_text FROM research_finding WHERE session_id = :sid"),
            {"sid": s.id},
        )
    ).all()
    assert [r[0] for r in dbFinding] == ["华东收货量下降 12%"]
    dbStatus = await dbSession.scalar(
        text("SELECT status FROM research_session WHERE id = :sid"), {"sid": s.id}
    )
    assert dbStatus == "done"

    with pytest.raises(ValueError):
        await svc.resolveCheckpoint(
            dbSession, checkpointId=uuid.uuid4(), status="confirmed", userChoice={}
        )
    with pytest.raises(ValueError):
        await svc.updateSessionStatus(dbSession, uuid.uuid4(), "done")
    with pytest.raises(ValueError):  # 状态白名单（设计枚举外的值）
        await svc.updateSessionStatus(dbSession, s.id, "completed")
    with pytest.raises(ValueError):
        await svc.saveFinding(
            dbSession,
            sessionId=s.id,
            turnId=turn.id,
            claimText="越界置信度",
            supportingSql=None,
            supportingData={},
            confidence=1.5,
        )
