import uuid

import pytest
from sqlalchemy import select

from app.domain.multi_step_models import MultiStepRun, MultiStepStep


@pytest.mark.asyncio
async def testResumeRejectsNonFailedRun(pg_client, db_session):
    # Arrange：一条 succeeded 的 run
    from app.services import multi_step_persistence as repo

    sessionKey = f"chat-{uuid.uuid4()}"
    # `datasourceId` 必须给：路由在 prepareResume **之前**有一道
    # `run.datasource_id is None ⇒ 409` 的守卫。不给就轮不到状态那道守卫 ——
    # 断言拿到 409 却完全没验到「状态不可续跑」这个本用例声称要验的东西（假绿）。
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=1,
        datasourceId=1, totalSteps=1,
    )
    await repo.updateRun(db_session, run, status="succeeded", finished=True)
    await db_session.commit()

    # Act
    resp = await pg_client.post(f"/api/v1/chat/multi-step/{run.id}/resume", json={})

    # Assert
    assert resp.status_code == 409
    # 两个 409 分支的文案不同（DomainError.message 经全局 handler 落到响应体
    # 的 `error` 字段），故必须钉住「是状态那道守卫拒的」，否则本用例对
    # 「把状态校验整段删掉」这种改动毫无反应。
    assert "not resumable" in resp.text, resp.text


@pytest.mark.asyncio
async def testResumeAdoptsExistingRunAndSkipsSucceededStep(pg_client, db_session, monkeypatch):
    """续跑走通 + **不新建第二个 run** + 跳过的成功步仍被算作已完成。

    这条用例是「最小正确版」裁决的回归闸，三个断言各堵一个真实缺陷：
    1. `len(allRuns) == 1` —— 原计划会把 resume 变成一次全新的 run（僵尸 + 重复）。
    2. `reloadedRun.status == "succeeded"` —— Task 6 的跳过分支若忘了把跳过的
       成功步计入 `completed`，`runStatusFor` 会把 run 判成 `failed`（用户看到
       「续跑又失败了」），而 `resume_count >= 1` 之类的弱断言完全发现不了。
    3. `steps[0].data` 仍是原值 —— 跳过分支若漏了，第 0 步会被重跑并覆盖结果。
       （对齐到下面的实际断言：钉的是 `data` 不是 `sql`。原 docstring 写的是
       `steps[0].sql`，与代码不符 —— 契约失真注释。）
    """
    import json

    from app.services import multi_step_persistence as repo
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _MULTI_STEP_PLAN_JSON,
        _install,
        _MultiStepLlm,
        _OkAdapter,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    # 播种的 sub_question **必须**与 Task 6 会算出的 subQuestions 逐字一致：Task 6 的
    # adoptRunForResume 以 sub_question 逐字相等判定「形状未变」，形状一变得归零整跑，
    # 本条用例的「跳过」断言就失效了。故这里**从同一个 `_MULTI_STEP_PLAN_JSON` 反推**，
    # 而不是手抄字符串 —— 注意 Task 6 的取值是 `description or subQuestion`（描述优先），
    # 手抄成 subQuestion 会静默对不上。
    question = "请分步查询 2024 和 2025 年的销售额并对比"
    planSteps = json.loads(_MULTI_STEP_PLAN_JSON)["steps"]
    subQuestions = [s.get("description") or s["subQuestion"] for s in planSteps]
    assert len(subQuestions) == 2

    sessionKey = f"chat-{uuid.uuid4()}"
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question=question,
        modelId=config.id, datasourceId=datasource.id, totalSteps=2,
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=subQuestions)
    await repo.finishStep(
        db_session, steps[0], status="succeeded",
        sql="SELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT GROUP BY NAME",
        data=[{"NAME": "A", "QTY": 10}],
    )
    await repo.recordStepError(db_session, steps[1], message="timeout", kind="transient")
    await repo.updateRun(
        db_session, run, status="failed", completedSteps=1, currentStepIdx=1, finished=True,
    )
    await db_session.commit()

    # Act
    resp = await pg_client.post(
        f"/api/v1/chat/multi-step/{run.id}/resume",
        json={"from_step_index": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    # Assert
    assert resp.status_code == 200, resp.text
    # ① 全程只有这一条 run（resume 复用而非新建）
    allRuns = (await db_session.execute(select(MultiStepRun))).scalars().all()
    assert len(allRuns) == 1, f"续跑不得新建 run，实际 {len(allRuns)} 条"
    assert allRuns[0].id == run.id

    reloaded = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == run.id).order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    # 必须逐行重读：请求在**另一个会话**里改这些行（pg_client 的 getDb 用的是它自己
    # 那个 session），而本会话的 identity map 仍缓存着续跑前的值 ——
    # `expire_on_commit=False` 不会让 `select()` 自动刷新已加载过的实例，于是这三条
    # 断言会读到「续跑前的世界」（last_error='timeout'、status='running'），
    # 用例红得像是实现错了。断言本身不动，只把读侧对齐到库里。
    for row in reloaded:
        await db_session.refresh(row)
    assert reloaded[0].status == "succeeded"
    assert reloaded[0].data == [{"NAME": "A", "QTY": 10}], "跳过的成功步不得被重跑覆盖"
    assert reloaded[1].last_error is None
    assert reloaded[1].status == "succeeded", "第 2 步应在续跑里跑成功"

    reloadedRun = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.id == run.id))
    ).scalar_one()
    await db_session.refresh(reloadedRun)
    assert reloadedRun.resume_count >= 1
    # ② 跳过的成功步计入 completed ⇒ 终态 succeeded（漏计会得到 failed）
    assert reloadedRun.status == "succeeded"
    assert reloadedRun.finished_at is not None, "续跑跑完必须封口，不能留下 running 僵尸"


@pytest.mark.asyncio
async def testResumeOutOfRangeFromStepIndexRejected(pg_client, db_session):
    """`from_step_index` 越界 → 409，且必须是**范围闸**拒的（不是状态/归属等同码分支）。

    这条正是本缺陷漏网的那条分支：前端曾给**汇总步**渲染续跑按钮，而汇总步的
    `fromStepIndex = len(data_steps)` —— 汇总步不落 `multi_step_step` 行，故该值
    恰好等于 `len(steps)`，恒越界（`start >= len(steps)`）。用户点下去只会拿到 409。
    前端已改为不渲染该按钮（MultiStepPlanCard），后端这条用例锁住「越界必须被拒、
    且文案能区分是谁拒的」。

    两个 409 分支文案不同：范围闸是 `from_step_index N out of range 0..M`，
    状态闸是 `run status ... not resumable`。只断言 409 等于没断言 ——
    删掉范围闸后请求会落到后续步骤守卫（`step N not completed`）或直接放行，
    状态码未必变，用例照样绿。
    """
    from app.services import multi_step_persistence as repo

    sessionKey = f"chat-{uuid.uuid4()}"
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=1,
        datasourceId=1, totalSteps=2,
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A", "查B"])
    # 两步都成功 ⇒ 排除「前序步未完成」那道守卫的干扰，确保被拒的只能是范围闸。
    for step in steps:
        await repo.finishStep(db_session, step, status="succeeded", data=[{"a": 1}])
    await repo.updateRun(db_session, run, status="failed", completedSteps=2, finished=True)
    await db_session.commit()

    # Act：from_step_index = len(steps) = 2 —— 汇总步按钮会发出的那个越界值
    resp = await pg_client.post(
        f"/api/v1/chat/multi-step/{run.id}/resume",
        json={"from_step_index": 2},
    )

    # Assert：先钉状态码 —— 越界若被放行，响应会变成 200 的 SSE 流
    # （StreamingResponse），此时 resp.json() 抛 JSONDecodeError，把「范围闸没拦住」
    # 这个真因伪装成一个解析错误。顺序反了会拿到难读的红。
    assert resp.status_code == 409, resp.text[:300]
    body = resp.json()
    assert "out of range 0..1" in body["error"], body
    assert "not resumable" not in body["error"], f"落到了状态闸：{body}"


@pytest.mark.asyncio
async def testPrepareResumeClearsStaleCompressedPayload(db_session):
    """从压缩步续跑必须清掉 data_compressed，否则留下「status=pending 但
    data_compressed 非空」的非法态（spec §5.3），且压缩钩子见非空即跳过
    ⇒ 该步此后永远无法再压缩。"""
    from app.services import multi_step_persistence as repo
    from app.services.multi_step_resume import prepareResume

    sessionKey = f"chat-{uuid.uuid4()}"
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question="q", modelId=1, totalSteps=2
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A", "查B"])
    # 前序步（step 0）**也**带压缩载荷：否则 `data_compressed` 本来就是 NULL，
    # 「前序步不被动」那条断言恒真（清没清都绿）。
    # 给 step 0 status=compressed 是 spec §5.3 的**合法**态（data_compressed 仅当
    # compressed 时有值），且 prepareResume 的前序步守卫恰好把它算作「已完成」。
    await repo.finishStep(db_session, steps[0], status="compressed", data=[{"a": 1}])
    steps[0].data_compressed = {"rows": 99}
    await repo.finishStep(db_session, steps[1], status="compressed", data=[{"b": 1}])
    steps[1].data_compressed = {"rows": 1}
    await repo.updateRun(db_session, run, status="failed", completedSteps=1, finished=True)
    await db_session.commit()

    # Act
    _run, start = await prepareResume(
        db_session, runId=run.id, fromStepIndex=1, idempotencyKey=None
    )

    # Assert
    assert start == 1
    reloaded = await repo.loadSteps(db_session, run.id)
    assert reloaded[1].status == "pending"
    assert reloaded[1].data_compressed is None
    # 前序步不动：状态与压缩载荷都必须是原值（重置范围必须从 start 起）。
    # 反向自检：把 prepareResume 里的 `step_index >= start` 改成 `>= 0` ⇒ 两条都红。
    assert reloaded[0].status == "compressed", "前序步不被动"
    assert reloaded[0].data_compressed == {"rows": 99}, "前序步不被动"


@pytest.mark.asyncio
async def testResumeIsIdempotentOnSameKey(pg_client, db_session, monkeypatch):
    from app.services import multi_step_persistence as repo
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _install,
        _MultiStepLlm,
        _OkAdapter,
    )

    config, datasource = await _seed(db_session)
    # 必须装多步 fake：第一次续跑会真的把流跑完，问句也得是多步问句，
    # 否则多步链路不进，run 无人封口（靠 Task 7 的 _sealAbandonedResume 兜底，
    # 但那条路径不该是本用例要验的幂等语义）。
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())
    question = "请分步查询 2024 和 2025 年的销售额并对比"

    sessionKey = f"chat-{uuid.uuid4()}"
    run = await repo.createRun(
        db_session, sessionId=sessionKey, question=question,
        modelId=config.id, datasourceId=datasource.id, totalSteps=1,
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A"])
    await repo.recordStepError(db_session, steps[0], message="x", kind="transient")
    await repo.updateRun(db_session, run, status="failed", finished=True)
    await db_session.commit()

    key = str(uuid.uuid4())
    first = await pg_client.post(
        f"/api/v1/chat/multi-step/{run.id}/resume", json={}, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 200, first.text

    afterFirst = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.id == run.id))
    ).scalar_one()
    await db_session.refresh(afterFirst)
    resumeCountAfterFirst = afterFirst.resume_count
    versionAfterFirst = afterFirst.version

    second = await pg_client.post(
        f"/api/v1/chat/multi-step/{run.id}/resume", json={}, headers={"Idempotency-Key": key}
    )

    # 必须钉住「是谁拒的」：两个分支都回 409（幂等键去重 / 状态不可续跑），
    # 只断言状态码等于没断言 —— 把去重闸删掉，第二次会落到状态闸，仍可能是 409 附近
    # 的码，用例照样绿。
    secondBody = second.json()
    assert second.status_code == 409, second.text
    assert "duplicate idempotency key" in secondBody["error"], secondBody
    assert "not resumable" not in secondBody["error"], (
        "必须是去重闸拒的；落到状态闸说明去重根本没生效"
    )

    reloadedRun = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.id == run.id))
    ).scalar_one()
    await db_session.refresh(reloadedRun)
    # 幂等的实证：第二次**没有**重新开局 —— resume_count / version 都不许再动。
    # 单看 `resume_count == 1` 是假绿（键从没被记下来时它也成立），必须与上面的
    # 409 去重断言合起来读：只有「被去重闸挡在 prepareResume 的写入之前」才推得出
    # 「两列都没变」。
    assert resumeCountAfterFirst == 1, "第一次续跑应恰好抬 1 次"
    assert reloadedRun.resume_count == 1, "第二次不得再抬 resume_count"
    assert reloadedRun.version == versionAfterFirst, "第二次不得再抬 version"
    assert key in (reloadedRun.idempotency_keys or [])


@pytest.mark.asyncio
async def testResumeUnknownRunReturns404(pg_client):
    """run 不存在 → 404，且必须是**缺席**那道拒的（不是状态/归属那些同码分支）。

    路由上有两个 404：`chat.py` 的 `NotFoundError(f"multi-step run {runId} 不存在")`
    与 `ResumeNotAllowed`（`prepareResume` 的 "run {runId} not found" 经同一 handler）。
    两者都会带 uuid，故只断言 404（或只断言含 uuid）区分不开 —— 文案里的
    「不存在」才是这条分支的指纹。
    """
    missingId = uuid.uuid4()
    resp = await pg_client.post(f"/api/v1/chat/multi-step/{missingId}/resume", json={})

    body = resp.json()
    assert resp.status_code == 404, resp.text
    assert str(missingId) in body["error"], body
    assert "不存在" in body["error"], body
    assert "not found" not in body["error"], f"落到了 ResumeNotAllowed 分支：{body}"
