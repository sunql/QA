import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.multi_step_models import MultiStepRun
from app.jobs.cleanup_multi_step_runs import cleanupMultiStepRuns
from app.tests import _pg_support


@pytest.fixture()
async def pgSession() -> AsyncIterator[AsyncSession]:
    """真实 PG 会话：每测试新建引擎 + TRUNCATE 隔离。

    本文件的断言是**全局**的（`deleted` 计数、剩余 run 集合），必须隔离。
    不能用 `db_session`：truncate 挂在 `pg_client` 上，`db_session` 不 truncate。
    """
    engine = await _pg_support._newEngine()
    try:
        await _pg_support._truncateAll(engine)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def testCleanupDeletesOnlyExpiredRuns(pgSession):

    sessionKey = f"chat-{uuid.uuid4()}"

    now = datetime.now(UTC)

    async def addRun(status: str, ageDays: int) -> MultiStepRun:
        run = MultiStepRun(
            id=uuid.uuid4(), session_id=sessionKey, question="q", model_id=None,
            total_steps=1, status=status,
            started_at=now - timedelta(days=ageDays),
            updated_at=now - timedelta(days=ageDays),
            finished_at=now - timedelta(days=ageDays),
        )
        pgSession.add(run)
        await pgSession.flush()
        return run

    oldSucceeded = await addRun("succeeded", 40)   # 删
    freshSucceeded = await addRun("succeeded", 5)  # 留
    oldFailed = await addRun("failed", 10)         # 删
    freshFailed = await addRun("failed", 2)        # 留
    oldRunning = await addRun("running", 100)      # 留（未终态不删）
    await pgSession.commit()

    # Act
    deleted = await cleanupMultiStepRuns(pgSession, now=now)
    await pgSession.commit()

    # Assert
    assert deleted == 2
    remaining = {r.id for r in (await pgSession.execute(select(MultiStepRun))).scalars().all()}
    assert remaining == {freshSucceeded.id, freshFailed.id, oldRunning.id}


@pytest.mark.asyncio
async def testCleanupDeletesStepsViaCascade(pgSession):
    from app.domain.multi_step_models import MultiStepStep

    sessionKey = f"chat-{uuid.uuid4()}"
    now = datetime.now(UTC)
    run = MultiStepRun(
        id=uuid.uuid4(), session_id=sessionKey, question="q", model_id=None,
        total_steps=1, status="succeeded",
        started_at=now - timedelta(days=60), updated_at=now - timedelta(days=60),
        finished_at=now - timedelta(days=60),
    )
    pgSession.add(run)
    await pgSession.flush()
    pgSession.add(MultiStepStep(id=uuid.uuid4(), run_id=run.id, step_index=0, status="succeeded", sub_question="a"))
    await pgSession.commit()

    await cleanupMultiStepRuns(pgSession, now=now)
    await pgSession.commit()

    assert (await pgSession.execute(select(MultiStepStep))).scalars().all() == []
