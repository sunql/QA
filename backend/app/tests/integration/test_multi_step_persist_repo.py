import uuid
from decimal import Decimal

import pytest

from app.services import multi_step_persistence as repo


@pytest.fixture
def sessionKey() -> str:
    """chat 侧会话 id 的自由字符串形态（前端是 `chat-<uuid>`，见 chatStore.ts:79）。

    session_id 是 String(64) 且无 FK（0115 裁决），所以**不再**需要先种一行
    ResearchSession —— 那正是旧设计（UUID + FK）唯一的用途。
    """
    return f"chat-{uuid.uuid4()}"


@pytest.mark.asyncio
async def testCreateRunAndSteps(db_session, sessionKey):
    # Act
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="两步题", modelId=3, totalSteps=2
    )
    steps = await repo.createSteps(
        db_session, runId=run.id, subQuestions=["查A", "查B"]
    )
    await db_session.commit()

    # Assert
    assert run.status == "running"
    assert run.total_steps == 2
    assert [s.step_index for s in steps] == [0, 1]
    assert all(s.status == "pending" for s in steps)
    assert [s.sub_question for s in steps] == ["查A", "查B"]


@pytest.mark.asyncio
async def testFinishStepWritesDataAndUsage(db_session, sessionKey):
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=3, totalSteps=1
    )
    (step,) = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A"])
    await repo.markStepRunning(db_session, step)

    await repo.finishStep(
        db_session, step, status="succeeded",
        sql="SELECT 1", data=[{"n": 1}], modelUsed="qwen", tokens=15, cost=0.0001,
    )
    await db_session.commit()

    loaded = (await repo.loadSteps(db_session, run.id))[0]
    assert loaded.status == "succeeded"
    assert loaded.data == [{"n": 1}]
    assert loaded.tokens_used == 15
    assert loaded.attempt_count == 0


@pytest.mark.asyncio
async def testRecordStepErrorAccumulatesAttempts(db_session, sessionKey):
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=3, totalSteps=1
    )
    (step,) = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A"])
    await repo.recordStepError(
        db_session, step, message="timeout", kind="transient", tokens=12, cost=0.0002
    )
    await repo.recordStepError(db_session, step, message="timeout again", kind="transient")
    await db_session.commit()

    loaded = (await repo.loadSteps(db_session, run.id))[0]
    assert loaded.attempt_count == 2
    assert loaded.last_error == "timeout again"
    assert loaded.last_error_kind == "transient"
    assert loaded.status == "running"  # 未终态
    # 失败尝试的用量累加不覆盖（spec §6.2）；第二次未传用量即按默认 0 处理
    assert loaded.tokens_used == 12
    assert loaded.cost == Decimal("0.0002")


@pytest.mark.asyncio
async def testUpdateRunClosesRun(db_session, sessionKey):
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=3, totalSteps=2
    )
    await repo.updateRun(
        db_session, run, status="partially_failed", completedSteps=1,
        currentStepIdx=1, errorSummary="第 2 步跳过", finished=True,
    )
    await db_session.commit()

    loaded = await repo.loadRun(db_session, run.id)
    assert loaded.status == "partially_failed"
    assert loaded.completed_steps == 1
    assert loaded.finished_at is not None


@pytest.mark.asyncio
async def testResetStepsFromClearsErrorsAndKeepsSucceeded(db_session, sessionKey):
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=3, totalSteps=3
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["a", "b", "c"])
    await repo.finishStep(db_session, steps[0], status="succeeded", data=[{"x": 1}], sql="SELECT 1")
    await repo.finishStep(db_session, steps[1], status="failed")
    await repo.recordStepError(db_session, steps[1], message="boom", kind="permanent")

    await repo.resetStepsFrom(db_session, runId=run.id, fromStepIndex=1)
    await db_session.commit()

    loaded = await repo.loadSteps(db_session, run.id)
    assert loaded[0].status == "succeeded" and loaded[0].data == [{"x": 1}]
    assert loaded[1].status == "pending" and loaded[1].last_error is None
    assert loaded[2].status == "pending"
