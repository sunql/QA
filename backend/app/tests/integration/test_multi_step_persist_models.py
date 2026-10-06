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
    # Arrange：裁决 A（migration 0115）把 session_id 定成**自由字符串**，真实生产者落
    # `chat-<uuid>`；它与 ResearchSession **无任何关系**（spec §12 明说 chat session 与
    # research_session 无关）。故这里既不建 ResearchSession 桩行，也不设外键 —— 那套
    # 是 0114 的旧契约（1c3515d 时 session_id 确实是 Mapped[uuid.UUID]）。
    sessionKey = f"chat-{uuid.uuid4()}"

    run = MultiStepRun(
        id=uuid.uuid4(),
        session_id=sessionKey,
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
    assert loaded.session_id == sessionKey, "自由字符串 session_id 必须原样往返"
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
    # 同 testRunAndStepRoundTrip：session_id 是自由字符串，不需要（也没有）ResearchSession 外键。
    run = MultiStepRun(
        id=uuid.uuid4(),
        session_id=f"chat-{uuid.uuid4()}",
        question="q",
        model_id=None,
        total_steps=1,
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
