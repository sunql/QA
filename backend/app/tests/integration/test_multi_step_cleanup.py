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
async def testCleanupDoesNotCollectTerminalRowWithoutFinishedAt(pgSession):
    """`failed + finished_at IS NULL` 的行**不回收** —— 这是显式裁定，不是遗漏。

    背景（IMP-1 方案 B）：修复前 `_recordStepFailure` 会把仍在执行的 run 写成
    `failed`，而循环中途 `token_usage_service.recordUsage` 的 `commit()` 会持久化这个
    中间态 ⇒ 历史上**确实可能**存在「终态但 `finished_at` 为空」的行。清理谓词两个分支
    都要求 `finished_at < cutoff`，故这类行（含历史遗留）**永不回收**。

    为什么**不**加 `COALESCE(finished_at, updated_at)` 兜底臂（决定与理由）：
      1. 方案 B 之后生产侧不再产生该形态 ⇒ 兜底臂对新行是死代码（`338f69b` 加过一版、
         `bc749d1` 以「该行态不可达」撤销；修复后那条理由重新成立）。
      2. 兜底臂会在**未来回归**（有人把写侧改回写终态）时把证据行连同其 steps
         级联删掉，掩盖回归；不回收时该行会一直可见，正是运维发现回归的信号。
      3. 需要真回收时，正确做法是先修写侧（本方案已做），而不是让清理器去猜。
    代价有界且已登记（summary.md 遗留项 16）：运维按
    `UPDATE multi_step_run SET finished_at = updated_at
       WHERE status IN ('succeeded','failed','partially_failed') AND finished_at IS NULL`
    一次性回填历史遗留行即可。

    反向自检：给谓词加 `COALESCE(finished_at, updated_at)` 兜底臂 ⇒ 本用例红
    （该行会被回收）。
    """

    sessionKey = f"chat-{uuid.uuid4()}"
    now = datetime.now(UTC)
    poisoned = MultiStepRun(
        id=uuid.uuid4(), session_id=sessionKey, question="q", model_id=None,
        total_steps=1, status="failed",
        started_at=now - timedelta(days=30), updated_at=now - timedelta(days=30),
        finished_at=None,
    )
    # 对照：同一时点的终态行，只差一个 finished_at ⇒ 必须被回收（证明谓词没坏）
    healthy = MultiStepRun(
        id=uuid.uuid4(), session_id=sessionKey, question="q", model_id=None,
        total_steps=1, status="failed",
        started_at=now - timedelta(days=30), updated_at=now - timedelta(days=30),
        finished_at=now - timedelta(days=30),
    )
    pgSession.add_all([poisoned, healthy])
    await pgSession.commit()

    # Act
    deleted = await cleanupMultiStepRuns(pgSession, now=now)
    await pgSession.commit()

    # Assert：只回收了对照行；缺 finished_at 的终态行原样保留（需人工回填）
    assert deleted == 1
    remaining = {
        r.id for r in (await pgSession.execute(select(MultiStepRun))).scalars().all()
    }
    assert remaining == {poisoned.id}, (
        "缺 finished_at 的终态行被回收了 —— 清理器不该替写侧兜底（会掩盖回归）"
    )


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
