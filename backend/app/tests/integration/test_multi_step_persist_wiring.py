import uuid

import pytest
from sqlalchemy import select

from app.domain.multi_step_models import MultiStepRun, MultiStepStep


@pytest.mark.asyncio
async def testMultiStepRunPersistedEndToEnd(pg_client, db_session, monkeypatch):
    """真链路：POST /api/v1/chat 走多步 → multi_step_run/step 落库。"""
    # **必须用 test_chat_multi_step 的 fake**：多步拆解由「查询拆分器」这个 system
    # prompt 分支驱动，只有 `_MultiStepLlm` 实现了它。`test_chat_api._PipelineLlm`
    # 没有该分支（落到 else 回一句自然语言）⇒ 根本不会产生多步计划 ⇒ 本用例会
    # 因为「一条 run 都没有」而红，且原因极具误导性。
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _install,
        _MultiStepLlm,
        _OkAdapter,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    # 这个问句是 test_chat_multi_step 里已验证会走多步、且拆出 2 步的那个。
    question = "请分步查询 2024 和 2025 年的销售额并对比"

    # Act
    resp = await pg_client.post(
        "/api/v1/chat", json=_payload(question, datasource.id)
    )

    # Assert
    assert resp.status_code == 200
    runs = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.question == question))
    ).scalars().all()
    assert len(runs) == 1, "多步跑完必须恰好落 1 条 run"
    steps = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == runs[0].id).order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    assert [s.step_index for s in steps] == [0, 1]
    assert all(s.status == "succeeded" for s in steps)
    assert all(s.sql for s in steps)


@pytest.mark.asyncio
async def testRunCountersShareOneDenominator(pg_client, db_session, monkeypatch):
    """IMP-7：`total_steps` / `completed_steps` / `current_step_idx` 三者同源。

    正常计划 = 2 个数据步 + 1 个汇总步。此前 `total_steps` 只数**数据步**（2），
    而 `completed_steps` 把末尾汇总步也算进去（3）⇒ 同一行落库成 `3/2`：已持久化的
    数据在说谎，任何对账/「未完成徽章」都会读到这个自相矛盾的行。

    流式与非流式**两条路径都断言**：口径漂移正是这类缺陷的复发形态。
    """
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _install,
        _MultiStepLlm,
        _OkAdapter,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())
    question = "请分步查询 2024 和 2025 年的销售额并对比"

    for path, sessionKey in (
        ("/api/v1/chat", "s-count-async"),
        ("/api/v1/chat/stream", "s-count-stream"),
    ):
        resp = await pg_client.post(
            path, json={**_payload(question, datasource.id), "sessionId": sessionKey}
        )
        assert resp.status_code == 200, f"{path}: {resp.text[:300]}"
        run = (
            await db_session.execute(
                select(MultiStepRun).where(MultiStepRun.session_id == sessionKey)
            )
        ).scalars().one()

        assert run.total_steps == 3, f"{path}: 2 数据步 + 1 汇总步 = 完整计划步数"
        assert run.completed_steps == run.total_steps, (
            f"{path}: 同一行里 {run.completed_steps}/{run.total_steps} —— 分子分母必须同源"
        )
        assert run.completed_steps <= run.total_steps, f"{path}: 完成数不得越过总数"
        # 收尾哨兵也走同一把尺子（越过末尾 = total_steps），三个计数列读起来互相自洽
        assert run.current_step_idx == run.total_steps, f"{path}: 收尾哨兵应等于总步数"
        assert run.status == "succeeded", f"{path}: {run.status}"


@pytest.mark.asyncio
async def testRunMarkedFailedWhenStepExhaustsRetries(pg_client, db_session, monkeypatch):
    """第 2 步 LLM 持续 ConnectError → run.status=failed，第 1 步仍 succeeded。"""
    import httpx

    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _install,
        _MultiStepLlm,
        _OkAdapter,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    import app.services.llm_retry_policy as retryPolicy
    monkeypatch.setattr(retryPolicy, "RETRY_WAIT_MIN_SECONDS", 0)
    monkeypatch.setattr(retryPolicy, "RETRY_WAIT_MAX_SECONDS", 0)
    monkeypatch.setattr(
        "app.services.multi_step_retry.TRANSIENT_WAITS", (0, 0)
    )

    callCount = {"n": 0}
    service = __import__("app.api.v1.chat", fromlist=["_service"])._service
    original = service._executeDataStep

    async def flakyStep(*args, **kwargs):
        callCount["n"] += 1
        if callCount["n"] >= 2:
            raise httpx.ConnectError("oMLX down")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_executeDataStep", flakyStep)

    question = "请分步查询 2024 和 2025 年的销售额并对比"
    resp = await pg_client.post("/api/v1/chat", json=_payload(question, datasource.id))

    assert resp.status_code in (200, 502, 503)
    run = (
        await db_session.execute(
            select(MultiStepRun).where(MultiStepRun.question == question)
        )
    ).scalars().one()
    assert run.status in ("failed", "partially_failed")
    steps = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == run.id).order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    assert steps[0].status in ("succeeded", "compressed")
    assert steps[-1].attempt_count >= 1
    assert steps[-1].status == "failed"   # spec §6.3：瞬态耗尽 → 步终态 failed
    assert steps[-1].last_error_kind == "transient"


@pytest.mark.asyncio
async def testPersistDisabledWritesNoRows(pg_client, db_session, monkeypatch):
    """kill switch 关掉 ⇒ 不落库，但多步本身照跑，且 `runId` 为 None。

    走**流式**而不是非流式：只断言 `runs == []` 的话，一个「压根没走多步」的
    装配也能让它通过（假绿）。这里用流式的 `multi_step_plan` 事件反过来钉住
    「多步确实跑了、且拆出 2 步」，同时顺带钉住 Task 9 依赖的 `runId: None` 分支。
    """
    from types import SimpleNamespace

    from app.services.stream_events import EVENT_MULTI_STEP_PLAN
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _install,
        _MultiStepLlm,
        _OkAdapter,
        _parseFrames,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.getSettings",
        lambda: SimpleNamespace(multiStepPersistEnabled=False),
    )

    resp = await pg_client.post(
        "/api/v1/chat/stream",
        json=_payload("请分步查询 2024 和 2025 年的销售额并对比", datasource.id),
    )

    assert resp.status_code == 200
    overview = [d for e, d in _parseFrames(resp) if e == EVENT_MULTI_STEP_PLAN]
    # 3 = 2 个数据步 + 1 个汇总步：`StepQueryPlanner` 无条件在每个计划的末尾注入
    # `aggregation_only=True` 的汇总步，`multi_step_plan` 的 steps 数组是**完整计划**。
    # 既有的 `test_chat_multi_step.test_stream_explicit_step_emits_step_events` 早已钉死
    # 这一点（`len(overview["steps"]) == 3` 且 `[aggregationOnly] == [False, False, True]`）。
    # （brief 原文写 == 2，RED 实测 `AssertionError: assert 3 == 2`。）
    assert len(overview[0]["steps"]) == 3
    assert overview[0]["runId"] is None, "开关关掉时没有 run，runId 必须是 None"

    runs = (await db_session.execute(select(MultiStepRun))).scalars().all()
    assert runs == []


@pytest.mark.asyncio
async def testStreamMultiStepPlanCarriesRunId(pg_client, db_session, monkeypatch):
    """流式 multi_step_plan 事件必须带 runId —— Task 9 前端续跑按钮的唯一来源。

    复用 test_chat_multi_step 的 fake 装配（同一个真实 API 链路）。断言分两层：
    键存在且是字符串，**并且**这个 id 真的能在库里查到 run —— 只断言字符串
    的话，随手 `str(uuid.uuid4())` 也能过。
    """
    from app.services.stream_events import EVENT_MULTI_STEP_PLAN
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _install,
        _MultiStepLlm,
        _OkAdapter,
        _parseFrames,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    # Act
    resp = await pg_client.post(
        "/api/v1/chat/stream",
        json=_payload("请分步查询 2024 和 2025 年的销售额并对比", datasource.id),
    )

    # Assert
    assert resp.status_code == 200, resp.text
    overview = [d for e, d in _parseFrames(resp) if e == EVENT_MULTI_STEP_PLAN]
    assert len(overview) == 1
    runId = overview[0]["runId"]
    assert isinstance(runId, str) and runId
    run = (
        await db_session.execute(
            select(MultiStepRun).where(MultiStepRun.id == uuid.UUID(runId))
        )
    ).scalar_one()
    assert run.question


@pytest.mark.asyncio
async def testStreamEmitsStepCompressedForEarlierStep(pg_client, db_session, monkeypatch):
    """压缩更早的步后补发 step_compressed —— Task 9「已压缩」徽章的唯一来源。"""
    from app.services.stream_events import EVENT_STEP_COMPRESSED
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _install,
        _MultiStepLlm,
        _parseFrames,
        _payload,
    )

    class _BigRowsAdapter:
        """固定返回 40 行：超过 DEFAULT_MAX_ROWS=30，让压缩比 != 1。

        行数必须真的超过保留上限，否则 originalRows == compressedRows，
        实现把两个数写反也照样通过。
        """

        def __init__(self) -> None:
            self.executed: list[str] = []

        async def execute_read_only(self, sql: str) -> list[dict]:
            self.executed.append(sql)
            return [{"NAME": f"N{i}", "QTY": i} for i in range(40)]

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _BigRowsAdapter())
    # 阈值恒真：只验证「压缩发生了 → 事件被发出来」，不依赖 token 估算的具体数值
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.shouldCompress", lambda *a, **k: True
    )

    # Act
    resp = await pg_client.post(
        "/api/v1/chat/stream",
        json=_payload("请分步查询 2024 和 2025 年的销售额并对比", datasource.id),
    )

    # Assert
    assert resp.status_code == 200, resp.text
    compressed = [d for e, d in _parseFrames(resp) if e == EVENT_STEP_COMPRESSED]
    # 只在处理第 2 步前压一次；_maybeCompressPriorSteps 对已有 data_compressed 的步会跳过
    assert len(compressed) == 1
    assert compressed[0]["stepIndex"] == 0
    assert compressed[0]["originalRows"] == 40
    assert compressed[0]["compressedRows"] == 30

    # 事件数字必须与落库的压缩结果一致（口径只有一个来源）
    step0 = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.step_index == 0)
        )
    ).scalars().one()
    assert step0.status == "compressed"
    assert step0.data_compressed["meta"]["original_rows"] == 40


@pytest.mark.asyncio
async def testSoftFailedStepRecordsErrorAndTerminalStatus(pg_client, db_session, monkeypatch):
    """**软失败**（不抛异常、返回 `sql=None` 错误行）同样落 failed 终态 + 错误记档。

    失败有两条路径：抛异常（走 except 分支，已有 `testRunMarkedFailedWhenStepExhaustsRetries`
    覆盖）与**软失败** —— 适配器执行失败时 `_executeDataStep` 内部回灌重试耗尽后
    **不抛异常**，而是返回一行 `sql=None` 的错误结果。后者同样必须落
    `last_error` / `last_error_kind` / `attempt_count`（spec §6.1）与 `failed`
    终态（spec §6.3）；漏了的话该步永远停在 `running`，且用户看得见失败、
    库里查不到原因。
    """
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _DataQueryFailAdapter,
        _install,
        _MultiStepLlm,
        _payload,
    )

    config, datasource = await _seed(db_session)
    # 第 2 个数据查询（step_index=1）起全部失败（含回灌重试那一次）⇒ 软失败错误行
    _install(
        monkeypatch, config, _MultiStepLlm(), _DataQueryFailAdapter(fail_from_query=2)
    )

    question = "请分步查询 2024 和 2025 年的销售额并对比"
    resp = await pg_client.post(
        "/api/v1/chat", json=_payload(question, datasource.id)
    )

    assert resp.status_code == 200, resp.text
    run = (
        await db_session.execute(
            select(MultiStepRun).where(MultiStepRun.question == question)
        )
    ).scalars().one()
    assert run.status == "failed"
    steps = (
        await db_session.execute(
            select(MultiStepStep)
            .where(MultiStepStep.run_id == run.id)
            .order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    assert steps[0].status == "succeeded"
    assert steps[1].status == "failed", "软失败必须落 failed 终态，不能停在 running"
    assert steps[1].last_error, "软失败同样要落错误文案（spec §6.1）"
    assert steps[1].last_error_kind == "permanent"
    assert steps[1].attempt_count == 1


@pytest.mark.asyncio
async def testAdoptRunForResumeAlignsShapeAndStart(pg_client, db_session):
    """`adoptRunForResume` 三个分支：形状一致保留起点 / 变长 / 变短。

    这是续跑唯一「不新建 run」的入口（最小正确版裁决）。三个分支分别对应：
    正常续跑、`model_override` 换了模型后重新规划出更多步、重新规划出更少步。
    """
    from app.services import multi_step_persistence as repo
    from app.services.multi_step_persistence import adoptRunForResume

    # session_id 是 chat 侧自由字符串（0115 裁决），无外键，不必种 ResearchSession。
    sessionKey = f"chat-{uuid.uuid4()}"
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=1, totalSteps=2
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A", "查B"])
    await repo.finishStep(
        db_session, steps[0], status="succeeded", sql="SELECT 1", data=[{"a": 1}]
    )
    await repo.recordStepError(db_session, steps[1], message="timeout", kind="transient")
    await repo.updateRun(db_session, run, status="failed", completedSteps=1, currentStepIdx=1)
    await db_session.commit()

    # --- 分支 1：形状一致 ⇒ 保留 current_step_idx，且**不动**已成功的第 0 步 ---
    adopted, start = await adoptRunForResume(
        db_session, runId=run.id, subQuestions=["查A", "查B"], totalSteps=2,
    )
    assert start == 1
    rows = await repo.loadSteps(db_session, run.id)
    assert rows[0].status == "succeeded", "已成功的更早步不能被重置（续跑就是靠它省掉重跑）"
    assert rows[0].sql == "SELECT 1", "更早步的 SQL 必须留着"
    assert rows[0].data == [{"a": 1}], "spec §5.3：data 永不删除"
    assert rows[1].status == "pending"
    assert rows[1].last_error is None
    assert rows[1].sql is None, "重跑会重新生成 SQL，留着旧的会污染将来的 sql_hash 复用"

    # --- 分支 2：计划变长（换了模型重新规划）⇒ 起点归零、补齐新行 ---
    # totalSteps 传 4（3 数据步 + 1 汇总步）**故意不等于** len(subQuestions)：口径是
    # 完整计划步数（IMP-7）。删掉 `run.total_steps = totalSteps` 就会残留旧值 2 ⇒ 红。
    adopted2, start2 = await adoptRunForResume(
        db_session, runId=run.id, subQuestions=["查X", "查Y", "查Z"], totalSteps=4,
    )
    assert start2 == 0, "形状变了，旧的「已完成」对应的是别的子问题，不能跳过任何步"
    assert adopted2.total_steps == 4, "分母必须按本次计划重刷（沿用旧值就是不同源）"
    rows = await repo.loadSteps(db_session, run.id)
    assert [s.step_index for s in rows] == [0, 1, 2]
    assert [s.sub_question for s in rows] == ["查X", "查Y", "查Z"]
    assert all(s.status == "pending" for s in rows)
    assert rows[0].sql is None, "形状变了 ⇒ 全跑，旧 SQL 必须清掉"

    # --- 分支 3：计划变短 ⇒ 删掉多余尾行（否则 stepsByIdx 里会留下对不上的孤儿） ---
    _adopted3, start3 = await adoptRunForResume(
        db_session, runId=run.id, subQuestions=["查X"], totalSteps=2,
    )
    assert start3 == 0
    assert _adopted3 is not None and _adopted3.total_steps == 2
    rows = await repo.loadSteps(db_session, run.id)
    assert [s.step_index for s in rows] == [0]
    assert rows[0].sub_question == "查X"

    # --- run 不存在（并发删除）⇒ (None, 0)，调用方回退普通路径，不炸 ---
    gone, start4 = await adoptRunForResume(
        db_session, runId=uuid.uuid4(), subQuestions=["查A"], totalSteps=1,
    )
    assert gone is None and start4 == 0


@pytest.mark.asyncio
async def testAdoptRunForResumeClampsOutOfRangePointer(pg_client, db_session):
    """写侧越界哨兵 ⇒ 读侧归零整跑（Important-2）。

    `_closeRun` 把 `current_step_idx` 当**越过末尾的哨兵**用（落
    `len(plan.steps)`，含汇总步 —— 比数据步数还大 1），而 `adoptRunForResume`
    原先把它当**合法起始下标**。直接走 `/api/v1/chat` 带 `resumeRunId` 命中一条
    已收尾的 run 时（该入口不经 `prepareResume` 覆盖），`startIndex` 会落到末尾
    之后 ⇒ 跳过分支把每一步都 continue 掉 ⇒ **一步不跑，run 却被重新封成终态**。

    越界与「形状不匹配」同处置：归零整跑。
    """
    from app.services import multi_step_persistence as repo
    from app.services.multi_step_persistence import adoptRunForResume

    sessionKey = f"chat-{uuid.uuid4()}"
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=1,
        totalSteps=3,   # 2 数据步 + 1 汇总步 —— 与下面哨兵 3 同一把尺子（IMP-7）
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A", "查B"])
    await repo.finishStep(
        db_session, steps[0], status="succeeded", sql="SELECT 1", data=[{"a": 1}]
    )
    await repo.finishStep(
        db_session, steps[1], status="succeeded", sql="SELECT 2", data=[{"b": 2}]
    )
    # 写侧 `_closeRun` 落的是 len(plan.steps)＝3（2 数据步 + 1 汇总步）—— 越界哨兵
    await repo.updateRun(
        db_session, run, status="succeeded", completedSteps=2, currentStepIdx=3
    )
    await db_session.commit()

    adopted, start = await adoptRunForResume(
        db_session, runId=run.id, subQuestions=["查A", "查B"], totalSteps=3,
    )
    assert adopted is not None
    assert adopted.total_steps == 3
    assert start == 0, "越界指针不是合法续跑起点 —— 必须归零整跑"
    rows = await repo.loadSteps(db_session, run.id)
    assert [s.step_index for s in rows] == [0, 1], "步照常对齐"
    assert all(s.status == "pending" for s in rows), "整跑：每步都重置为 pending"
    assert all(s.sql is None for s in rows), "整跑必须清掉旧 SQL"


@pytest.mark.asyncio
async def testResumeRunIdRejectsMalformedValue(pg_client, db_session, monkeypatch):
    """`/api/v1/chat` 是公开入参：畸形 `resumeRunId` 必须是 422，绝不是 500。

    Important-1 的边界层：字段类型是 `uuid.UUID | None`，Pydantic 在**请求边界**
    就把 `"abc"` 挡成 422 —— 不让它漏到 `uuid.UUID(...)` 变成未捕获的 ValueError。
    """
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _install,
        _MultiStepLlm,
        _OkAdapter,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    payload = _payload("请分步查询 2024 和 2025 年的销售额并对比", datasource.id)
    payload["resumeRunId"] = "abc"

    for path in ("/api/v1/chat", "/api/v1/chat/stream"):
        resp = await pg_client.post(path, json=payload)
        assert resp.status_code == 422, (
            f"{path}: 畸形 resumeRunId 必须 422，得到 {resp.status_code} —— "
            "未捕获的 ValueError 会变成 500"
        )


@pytest.mark.asyncio
async def testBeginRunForRequestDefensiveValidation(db_session):
    """Important-1 的纵深防御层：取值处再包一层 ⇒ 领域 ValidationError。

    字段类型已在边界挡过一次；这一层守的是「手工构造的 DTO / 别的调用方传进
    非 UUID 形态」——那种输入绕过 Pydantic 边界，必须收敛成领域 ValidationError
    （422），而不是未捕获的 ValueError（500）。只能绕过边界构造 DTO 来测。
    """
    from app.domain.exceptions import ValidationError
    from app.domain.schemas import ChatRequest

    dto = ChatRequest.model_construct(
        sessionId="s1",
        question="q",
        datasourceId=1,
        history=[],
        modelId=None,
        chartType=None,
        resumeRunId="not-a-uuid",
    )
    service = __import__("app.api.v1.chat", fromlist=["_service"])._service

    with pytest.raises(ValidationError):
        await service._beginRunForRequest(
            db_session, dto, subQuestions=["查A"], totalSteps=1
        )


@pytest.mark.asyncio
async def testHardFailureUsageAndParityAcrossPaths(pg_client, db_session, monkeypatch):
    """Important-3 + (c) 验收：同一失败输入 ⇒ 流式/非流式逐字段一致且用量留痕。

    构造：把 `_stepChart` 打补丁为抛错 —— 它在**生成阶段之后**才被调用，此时
    `_executeDataStep` 已累计了生成 token。异常从本方法逃逸。

    - Important-3 前：`_executeDataStep` 不把已花用量挂到异常上，调用点
      `getattr(exc, "tokens_used", 0)` 兜底成 0 ⇒ 失败步记 0（钱花了却漏计）。
    - (c)：两条路径共用同一批 mixin 方法 ⇒ 步记录必须逐字段相同。
    """
    import app.api.v1.chat as chat_module
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _install,
        _MultiStepLlm,
        _OkAdapter,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    async def _boom(*args, **kwargs):
        raise RuntimeError("chart stage exploded")

    monkeypatch.setattr(chat_module._service, "_stepChart", _boom)

    question = "请分步查询 2024 和 2025 年的销售额并对比"

    async def _runSteps(path: str, session_key: str) -> list[MultiStepStep]:
        resp = await pg_client.post(
            path,
            json={
                "sessionId": session_key,
                "question": question,
                "datasourceId": datasource.id,
            },
        )
        assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text[:300]}"
        runs = (
            await db_session.execute(
                select(MultiStepRun).where(MultiStepRun.session_id == session_key)
            )
        ).scalars().all()
        assert len(runs) == 1, f"{path}: 应恰好落 1 条 run，得到 {len(runs)}"
        return list(
            (
                await db_session.execute(
                    select(MultiStepStep)
                    .where(MultiStepStep.run_id == runs[0].id)
                    .order_by(MultiStepStep.step_index)
                )
            ).scalars().all()
        )

    asyncSteps = await _runSteps("/api/v1/chat", "s-nonstream")
    streamSteps = await _runSteps("/api/v1/chat/stream", "s-stream")

    assert len(asyncSteps) == 2, "计划有 2 个数据步"
    assert len(streamSteps) == 2

    # Important-3：硬失败路径的生成 token 必须留痕（钱已经花了）
    for path_label, steps in (("非流式", asyncSteps), ("流式", streamSteps)):
        for step in steps:
            assert step.status == "failed", f"{path_label}: 图表阶段硬失败 ⇒ 步 failed"
            assert step.tokens_used > 0, (
                f"{path_label}: 硬失败路径的生成 token 必须留痕，得到 {step.tokens_used}"
            )

    def _signature(steps: list[MultiStepStep]) -> list[tuple]:
        return [
            (s.status, s.tokens_used, s.cost, s.last_error_kind) for s in steps
        ]

    assert _signature(asyncSteps) == _signature(streamSteps), (
        "非流式与流式在同一次失败下必须产出逐字段一致的步记录：\n"
        f"  非流式={_signature(asyncSteps)}\n  流式={_signature(streamSteps)}"
    )
