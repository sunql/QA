import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.domain.multi_step_models import (
    STEP_STATUS_SUCCEEDED,
    MultiStepRun,
    MultiStepStep,
)


@pytest.mark.asyncio
async def testRunAndStepRoundTrip(db_session):
    # Arrange：先建一条 research_session 满足外键
    from app.domain.research_models import ResearchSession

    session_row = ResearchSession(id=uuid.uuid4(), title="msp-test", created_by=1)
    db_session.add(session_row)
    await db_session.flush()

    run = MultiStepRun(
        id=uuid.uuid4(),
        session_id=session_row.id,
        question="第一步查A，第二步查B",
        model_id=3,
        datasource_id=7,
        total_steps=2,
    )
    db_session.add(run)
    await db_session.flush()

    step = MultiStepStep(
        id=uuid.uuid4(),
        run_id=run.id,
        step_index=0,
        status=STEP_STATUS_SUCCEEDED,
        sub_question="查A",
        sql="SELECT 1 FROM dual",
        data=[{"a": 1}],
        tokens_used=15,
    )
    db_session.add(step)
    await db_session.commit()

    # Act
    loaded = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.id == run.id))
    ).scalar_one()

    # Assert
    assert loaded.status == "running"
    assert loaded.datasource_id == 7
    assert loaded.resume_count == 0
    assert loaded.version == 0
    assert loaded.idempotency_keys == []
    assert isinstance(loaded.started_at, datetime)

    steps = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == run.id)
        )
    ).scalars().all()
    assert len(steps) == 1
    assert steps[0].data == [{"a": 1}]
    assert steps[0].data_compressed is None
    assert steps[0].cost is None or steps[0].cost == 0


@pytest.mark.asyncio
async def testDuplicateStepIndexRejected(db_session):
    from app.domain.research_models import ResearchSession

    session_row = ResearchSession(id=uuid.uuid4(), title="msp-dup", created_by=1)
    db_session.add(session_row)
    await db_session.flush()
    run = MultiStepRun(
        id=uuid.uuid4(), session_id=session_row.id, question="q", model_id=None, total_steps=1
    )
    db_session.add(run)
    await db_session.flush()

    db_session.add_all([
        MultiStepStep(id=uuid.uuid4(), run_id=run.id, step_index=0, status="pending", sub_question="a"),
        MultiStepStep(id=uuid.uuid4(), run_id=run.id, step_index=0, status="pending", sub_question="b"),
    ])
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()
