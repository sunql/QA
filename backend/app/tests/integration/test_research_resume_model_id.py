"""spec N4 — service.resumeTurn 接受 model_id，写 session.model_id + audit。"""
import pytest
from sqlalchemy import select, text

from app.services.research_agent_service import ResearchAgentService
from app.services.research_session_service import ResearchSessionService


def _makeService():
    """服务工厂：fake 外部依赖 + 真实 ResearchSessionService / ReportPlanner。"""
    from app.services.report_planner import ReportPlanner
    from app.tests.integration.test_research_agent_service import (
        FakeChart,
        FakeEsl,
        FakeModelConfigs,
        FakeModelRouter,
        FakeOntology,
        FakePlanner,
        FakeRunner,
        FakeUsageRecorder,
    )
    sessions = ResearchSessionService()
    return ResearchAgentService(
        esl=FakeEsl(),
        sessionService=sessions,
        planner=FakePlanner(),
        runner=FakeRunner(),
        chartService=FakeChart(),
        usageRecorder=FakeUsageRecorder(),
        reporter=ReportPlanner(sessionService=sessions),
        autoConfirm=False,
        modelConfigs=FakeModelConfigs(),
        modelRouter=FakeModelRouter(),
        ontology=FakeOntology(),
        nl2sql=None,
    )


async def _ensureDatasource(dbSession):
    from app.domain.models import DataSource
    from app.domain.enums import DataSourceType
    from app.infrastructure.security.crypto import encryptApiKey
    existing = await dbSession.scalar(select(DataSource).limit(1))
    if existing is not None:
        return existing
    ds = DataSource(
        name="research-test-oracle",
        type=DataSourceType.POSTGRESQL,
        host="localhost",
        port=5432,
        database_name="qa_metadata",
        username="qa_user",
        password_encrypted=encryptApiKey("qa_pg_dev_2026"),
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def _newSession(svc, dbSession, modelId: int | None = None):
    ds = await _ensureDatasource(dbSession)
    return await svc.sessionService.createSession(
        dbSession, userId=1, question="供应商收货量为什么下降",
        datasourceId=ds.id, modelId=modelId,
    )


async def _startTurnAndPause(svc, dbSession, session):
    """启动 turn 并停在 checkpoint（模拟正常暂停点）。"""
    await svc.startTurn(dbSession, sessionId=session.id, question="供应商收货量为什么下降", userId=1)
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, session.id)
    return cp


@pytest.mark.asyncio
async def test_resume_turn_with_model_id_updates_session(dbSession):
    """传 model_id=8 → session.model_id=8 + audit 写。"""
    svc = _makeService()
    session = await _newSession(svc, dbSession, modelId=None)
    checkpoint = await _startTurnAndPause(svc, dbSession, session)

    await svc.resumeTurn(
        dbSession,
        checkpointId=checkpoint.id,
        action="confirm",
        choice={},
        model_id=8,
    )

    # 验证 session.model_id 已更新
    from app.domain.research_models import ResearchSession
    session_row = await dbSession.get(ResearchSession, checkpoint.session_id)
    assert session_row.model_id == 8, f"session.model_id 应为 8，实际 {session_row.model_id}"

    # 验证 audit_log 写入了（用显式列名避免 index 错位）
    audit_row = await dbSession.execute(
        select(
            text("entity_type"),
            text("entity_id_str"),
            text("action"),
            text("before_json"),
            text("after_json"),
        ).select_from(text("audit_log")).where(
            text("entity_type = 'research_turn'"),
            text(f"entity_id_str = '{checkpoint.turn_id}'"),
            text("action = 'UPDATE'"),
        ).order_by(text("created_at desc")).limit(1)
    )
    audit = audit_row.fetchone()
    assert audit is not None, "期望 audit_log 有一条 UPDATE 记录"
    entity_type, entity_id_str, action, before_json, after_json = audit
    assert action == "UPDATE"
    assert after_json == {"model_id": 8}, f"after_json 应为 {{model_id:8}}，实际 {after_json}"


@pytest.mark.asyncio
async def test_resume_turn_without_model_id_keeps_session_model(dbSession):
    """不传 model_id → session.model_id 保持 None。"""
    svc = _makeService()
    session = await _newSession(svc, dbSession, modelId=None)
    checkpoint = await _startTurnAndPause(svc, dbSession, session)

    await svc.resumeTurn(
        dbSession,
        checkpointId=checkpoint.id,
        action="confirm",
        choice={},
    )

    from app.domain.research_models import ResearchSession
    session_row = await dbSession.get(ResearchSession, checkpoint.session_id)
    assert session_row.model_id is None, f"session.model_id 应保持 None，实际 {session_row.model_id}"


@pytest.mark.asyncio
async def test_resume_turn_with_same_model_id_no_audit(dbSession):
    """传 model_id 等于 session.model_id → 不写 audit（避免冗余）。"""
    svc = _makeService()
    session = await _newSession(svc, dbSession, modelId=None)
    checkpoint = await _startTurnAndPause(svc, dbSession, session)

    # 先记下基线
    result = await dbSession.execute(
        select(text("count(*)")).select_from(text("audit_log")).where(
            text("entity_type = 'research_turn'"),
            text(f"entity_id_str = '{checkpoint.turn_id}'"),
        )
    )
    baseline_count = result.scalar_one()

    # 用当前 session.model_id 续跑（相同值，不应写 audit）
    from app.domain.research_models import ResearchSession
    session_row = await dbSession.get(ResearchSession, checkpoint.session_id)
    current_model_id = session_row.model_id

    await svc.resumeTurn(
        dbSession,
        checkpointId=checkpoint.id,
        action="confirm",
        choice={},
        model_id=current_model_id,
    )

    # 验证 audit 没有新增
    result = await dbSession.execute(
        select(text("count(*)")).select_from(text("audit_log")).where(
            text("entity_type = 'research_turn'"),
            text(f"entity_id_str = '{checkpoint.turn_id}'"),
        )
    )
    assert result.scalar_one() == baseline_count, "model_id 未变时不应写 audit"


@pytest.mark.asyncio
async def test_resume_turn_with_equal_model_id_no_audit(dbSession):
    """model_id 非 None 但等于 session.model_id → 不写 audit, 不更新。"""
    svc = _makeService()
    session = await _newSession(svc, dbSession, modelId=None)
    checkpoint = await _startTurnAndPause(svc, dbSession, session)

    # 把 session.model_id 设为 7（模拟 W5 会话级选中的模型）
    from app.domain.research_models import ResearchSession
    session_row = await dbSession.get(ResearchSession, checkpoint.session_id)
    session_row.model_id = 7
    await dbSession.flush()

    # 基线
    result = await dbSession.execute(
        select(text("count(*)")).select_from(text("audit_log")).where(
            text("entity_type = 'research_turn'"),
            text(f"entity_id_str = '{checkpoint.turn_id}'"),
        )
    )
    baseline_count = result.scalar_one()

    # model_id=7 等于 session.model_id=7，条件 "model_id is not None and != " 不满足
    await svc.resumeTurn(
        dbSession,
        checkpointId=checkpoint.id,
        action="confirm",
        choice={},
        model_id=7,
    )

    # 验证 session.model_id 未变
    session_row = await dbSession.get(ResearchSession, checkpoint.session_id)
    assert session_row.model_id == 7, f"session.model_id 应保持 7，实际 {session_row.model_id}"

    # 验证 audit 没有新增
    result = await dbSession.execute(
        select(text("count(*)")).select_from(text("audit_log")).where(
            text("entity_type = 'research_turn'"),
            text(f"entity_id_str = '{checkpoint.turn_id}'"),
        )
    )
    assert result.scalar_one() == baseline_count, "model_id 相等时不应写 audit"


@pytest.mark.asyncio
async def test_resume_turn_raises_on_nonexistent_checkpoint(dbSession):
    """checkpoint 不存在 → ValueError。"""
    import uuid
    svc = _makeService()
    session = await _newSession(svc, dbSession, modelId=None)

    with pytest.raises(ValueError, match="checkpoint 不存在"):
        await svc.resumeTurn(
            dbSession,
            checkpointId=uuid.uuid4(),
            action="confirm",
            choice={},
        )
