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


@pytest.mark.asyncio
async def testAppendIdempotencyKeyDedupesWithoutLosingConcurrentKey(db_session, sessionKey):
    """幂等键追加必须**原子**且幂等（IMP-5）。

    裸的读改写会丢键：两个并发写者各自把 `existing + [key]` 落库，后写的整体覆盖
    先写的（LAST WRITE WINS）—— 先写那个键凭空消失，之后它再来就骗过去重闸。
    本用例用**两个真实会话**造出这个交错：另一个会话先落 `key-b`，本会话的 ORM 实例
    仍是陈旧的（`expire_on_commit=False`，没人刷新它），随后本会话追加 `key-a`。
    旧实现（直接读 `run.idempotency_keys`）会写出 `["key-a"]`，`key-b` 被抹掉。

    去重 no-op 分支同样承重：同一键重复提交不得重复入列（否则该列无界增长）。
    反向自检：把 appendIdempotencyKey 改回裸读改写 ⇒ 第一条断言红（只剩 key-a）。
    """
    from app.infrastructure import database as dbModule

    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=3, totalSteps=1
    )
    await db_session.commit()

    # 另一个会话 = 另一个写者，先追加 key-b 并提交
    factory = dbModule.getSessionFactory()
    async with factory() as other:
        otherRun = await repo.loadRun(other, run.id)
        await repo.appendIdempotencyKey(other, otherRun, "key-b")
        await other.commit()

    # 本会话（ORM 实例陈旧）追加 key-a，然后再追加一次 key-a 走去重 no-op
    await repo.appendIdempotencyKey(db_session, run, "key-a")
    await db_session.commit()
    await repo.appendIdempotencyKey(db_session, run, "key-a")
    await db_session.commit()

    reloaded = await repo.loadRun(db_session, run.id)
    await db_session.refresh(reloaded)
    assert sorted(reloaded.idempotency_keys or []) == ["key-a", "key-b"], (
        f"并发写者的键被覆盖或重复入列：{reloaded.idempotency_keys}"
    )


@pytest.mark.asyncio
async def testPrepareResumeSerializesConcurrentResumeWithRowLock(db_session, sessionKey):
    """续跑守门必须被行锁**串行化**（IMP-5）。

    没有锁时两个并发续跑都读到「status=failed」，双双通过守门 ⇒ 同一步跑两遍
    （双份 LLM 花费 + 两路交错写同一 run/steps）。本用例把交错固定下来：

      1. 会话 A 用 `loadRun(forUpdate=True)` 持有该行的锁（模拟第一个续跑已取到锁、
         尚未提交）；
      2. 会话 B 起 `prepareResume` —— 它必须**阻塞**在 `SELECT … FOR UPDATE` 上；
      3. A 把 run 置回 running 并提交（= 第一个续跑开跑）；
      4. B 拿到锁后读到的是 running ⇒ 被状态闸拒。

    「B 没跑完」这一步就是判别式：去掉 `with_for_update()`，B 不会阻塞、读到陈旧的
    failed ⇒ 一路放行、无异常 ⇒ `pytest.raises` 落空（DID NOT RAISE）而红。
    """
    import asyncio

    from app.infrastructure import database as dbModule
    from app.services.multi_step_resume import ResumeConflict, prepareResume

    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=3,
        datasourceId=1, totalSteps=1,
    )
    (step,) = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A"])
    await repo.finishStep(db_session, step, status="succeeded", data=[{"a": 1}])
    await repo.updateRun(db_session, run, status="failed", completedSteps=1, finished=True)
    await db_session.commit()

    factory = dbModule.getSessionFactory()
    async with factory() as first, factory() as second:
        # 1) A 持锁
        await repo.loadRun(first, run.id, forUpdate=True)

        # 2) B 起续跑
        task = asyncio.create_task(
            prepareResume(second, runId=run.id, fromStepIndex=1, idempotencyKey=None)
        )
        await asyncio.sleep(0.3)
        assert not task.done(), (
            "第二个续跑没有在行锁上等待就返回了 —— 守门没有被串行化（双跑风险）"
        )

        # 3) A 提交「已开跑」
        firstRun = await repo.loadRun(first, run.id)
        await repo.updateRun(first, firstRun, status="running")
        await first.commit()

        # 4) B 现在应当看到 running 并被拒
        with pytest.raises(ResumeConflict) as excinfo:
            await asyncio.wait_for(task, timeout=10)
        assert "not resumable" in str(excinfo.value), str(excinfo.value)
