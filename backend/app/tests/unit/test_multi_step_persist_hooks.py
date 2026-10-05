from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.multi_step_persist_hooks import MultiStepPersistMixin


class _Host(MultiStepPersistMixin):
    pass


@pytest.mark.asyncio
async def testPersistDisabledSkipsEverything(monkeypatch):
    # Arrange
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.getSettings",
        lambda: SimpleNamespace(multiStepPersistEnabled=False),
    )
    host = _Host()
    session = AsyncMock()
    createRun = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.createRun", createRun
    )

    # Act
    run = await host._openRun(
        session, sessionId="s", question="q", modelId=1, subQuestions=["a"]
    )

    # Assert
    assert run is None
    createRun.assert_not_awaited()


@pytest.mark.asyncio
async def testPersistEnabledOpensRun(monkeypatch):
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.getSettings",
        lambda: SimpleNamespace(multiStepPersistEnabled=True),
    )
    host = _Host()
    session = AsyncMock()
    # 必须带 id：_openRun 紧接着要把它喂给 createSteps(runId=run.id)
    run = SimpleNamespace(id="r1")
    createRun = AsyncMock(return_value=run)
    createSteps = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.createRun", createRun
    )
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.createSteps", createSteps
    )

    result = await host._openRun(
        session, sessionId="s", question="q", modelId=1, subQuestions=["a"]
    )

    assert result is run
    assert createRun.await_args.kwargs["totalSteps"] == 1
    assert createSteps.await_args.kwargs["runId"] == "r1"


@pytest.mark.asyncio
async def testPersistStepFailureRecordsClassification(monkeypatch):
    host = _Host()
    session = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.recordStepError", record
    )
    updateRun = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.updateRun", updateRun
    )

    kind = await host._persistStepFailure(
        session, SimpleNamespace(step_index=1), httpx.ConnectError("refused"), run=SimpleNamespace()
    )

    assert kind == "transient"
    assert record.await_args.kwargs["kind"] == "transient"
    assert record.await_args.kwargs["tokens"] == 0
    assert record.await_args.kwargs["cost"] == 0
    assert updateRun.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def testPersistStepFailureWithStepNoneSkipsWriteButStillClassifies(monkeypatch):
    """kill switch 关掉时 step 为 None：只分类、不落库。

    缺这个守卫会让 recordStepError 在 `step.attempt_count` 抛 AttributeError，
    把原始的步错误顶掉 —— 关掉开关反而崩在守卫自身。传了 run 是为了同时钉住
    `if run is not None` 分支里的 `step.step_index` 访问也被早返回保护。
    """
    host = _Host()
    session = AsyncMock()
    record = AsyncMock()
    updateRun = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.recordStepError", record
    )
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.updateRun", updateRun
    )

    kind = await host._persistStepFailure(
        session, None, httpx.ConnectError("refused"),
        run=SimpleNamespace(status="running"),
    )

    assert kind == "transient"
    record.assert_not_awaited()
    updateRun.assert_not_awaited()


@pytest.mark.asyncio
async def testMaybeCompressNoOpWhenUnderThreshold(monkeypatch):
    host = _Host()
    session = AsyncMock()
    finishStep = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.finishStep", finishStep
    )
    steps = [SimpleNamespace(step_index=0, status="succeeded", data=[{"a": 1}], data_compressed=None)]

    changed = await host._maybeCompressPriorSteps(
        session, SimpleNamespace(), steps,
        nextStepIdx=1, maxInputTokens=100000, injectionText="短文本",
    )

    assert changed is False
    finishStep.assert_not_awaited()


@pytest.mark.asyncio
async def testMaybeCompressCompressesPriorSucceededSteps(monkeypatch):
    host = _Host()
    session = AsyncMock()
    finishStep = AsyncMock()
    updateRun = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.finishStep", finishStep
    )
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.updateRun", updateRun
    )
    run = SimpleNamespace(id="r1", compressed_count=0)
    steps = [
        # 唯一符合「前于 nextStepIdx + succeeded + 有 data + 未压缩过」的步
        SimpleNamespace(
            step_index=0, status="succeeded",
            data=[{"a": i} for i in range(50)], data_compressed=None,
        ),
        SimpleNamespace(step_index=1, status="succeeded", data=None, data_compressed=None),
        SimpleNamespace(step_index=2, status="failed", data=[{"a": 1}], data_compressed=None),
        SimpleNamespace(step_index=3, status="succeeded", data=[{"a": 1}], data_compressed=None),
    ]

    # maxInputTokens=10 且注入文本远超阈值 ⇒ 必压
    changed = await host._maybeCompressPriorSteps(
        session, run, steps,
        nextStepIdx=3, maxInputTokens=10, injectionText="很长的注入文本" * 20,
    )

    assert changed is True
    # step 1 无 data、step 2 非成功态、step 3 不在 nextStepIdx 之前 ⇒ 只压 step 0
    assert finishStep.await_count == 1
    assert finishStep.await_args.args[1] is steps[0]
    assert finishStep.await_args.kwargs["status"] == "compressed"
    assert steps[0].data_compressed is not None
    assert steps[0].data_compressed["meta"]["original_rows"] == 50
    # 原始 data 永不被删除（spec §5：压缩结果另存 data_compressed）
    assert steps[0].data is not None and len(steps[0].data) == 50
    assert updateRun.await_args.kwargs["compressedCount"] == 1
