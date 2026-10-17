"""research_session_service 持久化链路（真实 PostgreSQL，串行）。

Task 3 契约测试：会话 / 轮次 / checkpoint / finding / report 五张表的写入链路。
ORM 列属性为 snake_case（见 app/domain/research_models.py），故断言用 snake_case。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.domain.research_models import ResearchSession
from app.services.research_session_service import CheckpointStaleError, ResearchSessionService


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
async def test_resolve_checkpoint_is_atomic_against_stale_read(dbSession) -> None:
    """MEDIUM：并发下第二个决策者必须被原子 UPDATE 拒绝（TOCTOU 封堵）。

    模拟并发：本会话 identity map 里 checkpoint 仍是 pending，而库里已被另一决策者
    改成 rejected。旧实现只按内存值判断，会放行并**覆盖**先到者的决策（双跑状态机）；
    新实现的条件 UPDATE（`WHERE status='pending'`）rowcount=0 ⇒ 抛既有 ValueError。
    """
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})
    cp = await svc.openCheckpoint(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        phase="intent",
        options={"resumePhase": "plan"},
        prompt="p",
    )
    # 另一决策者抢先落库（原始 UPDATE 不刷新本会话 identity map，正是并发现场）
    await dbSession.execute(
        text(
            "UPDATE research_checkpoint SET status = 'rejected',"
            " user_choice = CAST(:choice AS jsonb) WHERE id = :cid"
        ),
        {"cid": cp.id, "choice": '{"by": "B"}'},
    )

    with pytest.raises(ValueError, match="非 pending"):
        await svc.resolveCheckpoint(
            dbSession, checkpointId=cp.id, status="confirmed", userChoice={"by": "A"}
        )

    # 先到者的决策原样保留（未被后来者覆盖）
    row = (
        await dbSession.execute(
            text("SELECT status, user_choice FROM research_checkpoint WHERE id = :cid"),
            {"cid": cp.id},
        )
    ).one()
    assert row.status == "rejected" and row.user_choice == {"by": "B"}


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


@pytest.mark.asyncio
async def test_save_finding_supports_datetime_and_decimal_rows(dbSession) -> None:
    """supportingData["rows"] 含 datetime/Decimal 时不抛 TypeError。

    2026-10-09 复盘 3188a28c + 25d597a6：Oracle 19c 真机数据里常带 DATE/TRUNC
    列（datetime）和 number(p,s)（Decimal）。SQLAlchemy asyncpg 默认 JSON 编码
    不认这两种类型，整条 finding INSERT 抛 ``TypeError: Object of type
    datetime is not JSON serializable``，verify 阶段整段崩，session 被打 failed
    标，前端看到"无提示 + 报告不存在"。

    本契约：``saveFinding`` 在边界把 datetime / Decimal 强制转 ISO 字符串 / float
    入库，**不**要求 caller 先手工 coerce（与项目 `json.dumps(..., default=str)`
    的 5+ 处既有约定一致）。
    """
    from datetime import datetime as _dt
    from decimal import Decimal as _Dec

    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})

    dt = _dt(2026, 10, 9, 12, 30, 45)
    dec = _Dec("0.4567")
    payload = {
        "rows": [
            {"month_id": dt, "cnt": 12, "share": dec},
            {"month_id": dt, "cnt": 8, "share": _Dec("0.3")},
        ],
        "rowCount": 2,
        "error": None,
        "sql": "SELECT TRUNC(ARR_DATE, 'MM') AS month_id, COUNT(*) AS cnt FROM DWD_ARRIVAL_ORDER_DTL",
    }
    finding = await svc.saveFinding(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        claimText="到货量月度趋势下降 33%",
        supportingSql=payload["sql"],
        supportingData=payload,
        confidence=0.45,
    )
    assert finding.id is not None
    # DB 真值断言：datetime/Decimal 应**已**转 ISO/float 落库（不抛）
    dbRow = (
        await dbSession.execute(
            text("SELECT supporting_data FROM research_finding WHERE id = :id"),
            {"id": finding.id},
        )
    ).first()
    assert dbRow is not None
    sd = dbRow[0]
    assert isinstance(sd, dict)
    assert sd["rowCount"] == 2
    first = sd["rows"][0]
    assert isinstance(first["month_id"], str), (
        f"datetime 应被 ISO 化, 实得: {type(first['month_id']).__name__}"
    )
    assert first["month_id"].startswith("2026-10-09"), first["month_id"]
    assert isinstance(first["share"], float), (
        f"Decimal 应被 float 化, 实得: {type(first['share']).__name__}"
    )


@pytest.mark.asyncio
async def test_resolve_checkpoint_rejects_stale(dbSession) -> None:
    """expires_at 已过的 checkpoint 视为已非 pending。"""
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})
    cp = await svc.openCheckpoint(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        phase="intent",
        options={"resumePhase": "plan"},
        prompt="p",
        expiresAt=datetime.now(UTC) - timedelta(hours=1),
    )
    assert cp.status == "pending"
    with pytest.raises(CheckpointStaleError, match="过期"):
        await svc.resolveCheckpoint(dbSession, checkpointId=cp.id, status="confirmed", userChoice={})


@pytest.mark.asyncio
async def test_resolve_checkpoint_writes_audit_log(dbSession) -> None:
    """checkpoint answer 后 audit_log 应有对应记录。"""
    from app.domain.models import AuditLog

    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})
    cp = await svc.openCheckpoint(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        phase="intent",
        options={},
        prompt="p",
    )
    await svc.resolveCheckpoint(
        dbSession, checkpointId=cp.id, status="confirmed", userChoice={"selectedIndexes": [0, 1]}, userId=1
    )
    # checkpoint.id 是 UUID → audit_log 走 entity_id_str 列（0117），entity_id 为 NULL。
    row = await dbSession.execute(
        text("SELECT entity_type, entity_id, entity_id_str, action, actor, after_json FROM audit_log WHERE entity_type = 'research_checkpoint'")
    )
    log_row = row.one()
    assert log_row.entity_type == "research_checkpoint"
    assert log_row.entity_id is None
    assert log_row.entity_id_str == str(cp.id)
    assert log_row.action == "UPDATE"
    assert log_row.after_json["status"] == "confirmed"


@pytest.mark.asyncio
async def test_save_finding_with_enhanced_claim_and_stats(dbSession) -> None:
    """enhancedClaim 优先于 claimText；stats 合并到 supporting_data["stats"]。

    覆盖 R2 plan 任务 2：verify 阶段把聚合结果 + 数据视角结论回填到 finding 行。
    """
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="Q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})
    row = await svc.saveFinding(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        claimText="原 hypothesis（应被 enhancedClaim 覆盖）",
        supportingSql="SELECT 1",
        supportingData={"rowCount": 1, "rows": [], "error": None},
        confidence=0.5,
        enhancedClaim="按基地 J1 占 78%",
        stats={
            "topContributors": [
                {"dim": "base", "value": "J1", "sharePct": 78.0, "count": 12},
            ],
            "distribution": {"amount": {"min": 10.0, "max": 100.0, "mean": 55.0, "std": 30.0}},
            "mom": (), "anomalies": (),
            "numericColumns": ("amount",), "dimensionColumns": ("base",), "rowCount": 12,
        },
    )
    await dbSession.commit()
    assert row.claim_text == "按基地 J1 占 78%"
    assert row.supporting_data["rowCount"] == 1  # 原 keys 保留
    assert row.supporting_data["stats"]["topContributors"][0]["dim"] == "base"
    assert row.supporting_data["stats"]["distribution"]["amount"]["max"] == 100.0


@pytest.mark.asyncio
async def test_save_finding_backward_compatible_when_new_kwargs_omitted(dbSession) -> None:
    """旧调用形态（5 个必填 + 2 必填 kwarg）必须仍工作，claim_text 走原 claimText。"""
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="Q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})
    row = await svc.saveFinding(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        claimText="原 claim 不变",
        supportingSql="SELECT 1",
        supportingData={"rowCount": 1, "rows": [], "error": None},
        confidence=0.5,
    )
    await dbSession.commit()
    assert row.claim_text == "原 claim 不变"
    assert "stats" not in (row.supporting_data or {})


# ---------------------------------------------------------------------------
# X3（1012 cross-refs spec §P0-X3）：openCheckpoint 接受 originalQuestion
# 固化在 checkpoint 本体；恢复阶段读这一列而不再去 join turn.content。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openCheckpoint_persistsOriginalQuestion(dbSession) -> None:
    """X3：openCheckpoint 落 original_question，DB 真值断言 + DTO 序列化断言。"""
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})
    cp = await svc.openCheckpoint(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        phase="intent",
        options={"arms": ["bo"]},
        prompt="三臂是否齐全？",
        originalQuestion="对比 2024 和 2025 年销售额",
    )
    await dbSession.commit()
    assert cp.original_question == "对比 2024 和 2025 年销售额", (
        f"originalQuestion 应落 DB，实际 {cp.original_question!r}"
    )
    # DTO 序列化断言（X3 Minor review 项）：防止 CamelModel alias_generator
    # 配置漂移让前端读不到 originalQuestion
    from app.api.v1.research import _checkpointRead
    dto = _checkpointRead(cp)
    assert dto.originalQuestion == "对比 2024 和 2025 年销售额", (
        f"ResearchCheckpointRead.originalQuestion 应驼峰映射到 row.original_question，"
        f"实际 {dto.originalQuestion!r}"
    )
    # 反向断言：旧调用（不传）也不破坏，存为空串
    cp2 = await svc.openCheckpoint(
        dbSession,
        sessionId=s.id,
        turnId=turn.id,
        phase="intent",
        options={"arms": ["bo"]},
        prompt="p",
    )
    await dbSession.commit()
    assert cp2.original_question == "", (
        f"未传 originalQuestion 应默认为空串，实际 {cp2.original_question!r}"
    )
    dto2 = _checkpointRead(cp2)
    assert dto2.originalQuestion == "", (
        f"未传时 DTO.originalQuestion 也应为空串，实际 {dto2.originalQuestion!r}"
    )
