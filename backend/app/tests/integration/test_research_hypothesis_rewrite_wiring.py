"""A2 (2026-10-08)：``_stageVerify`` 拦截后单次 LLM 重写的接线测试。

2026-10-08 复盘：研究假设 SQL 被方言守卫拦到 LIMIT/INTERVAL 等违规语法时，
``runVerification`` 只短路写 error，**不**把违规原因喂回 LLM 重写 → finding
`rows=[]` → 报告 data 章节无 chart。本测试验证 ``_stageVerify`` 接入
``rewriteHypothesisSqlWithHint`` 后，**单次**重写 → 重跑 → 用重写后结果落库。

跑命令（独立测试库 5434）：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test \\
        uv run pytest app/tests/integration/test_research_hypothesis_rewrite_wiring.py -v
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.research_agent_execution import buildExecutionDeps
from app.services.research_agent_phases import ResearchAgentPhasesMixin


# ---- 固定副本 ---------------------------------------------------------------

_HYPOTHESIS_WITH_LIMIT = {
    "statement": "近 12 个月收货量趋势",
    "driver": "收货量",
    "verificationSql": (
        "SELECT rcv_month, SUM(rcv_qty) AS total "
        "FROM DWD_GOODS_RECEIPT_DTL "
        "GROUP BY rcv_month ORDER BY rcv_month DESC LIMIT 12"
    ),
}

_REWRITTEN_FETCH_FIRST = (
    "SELECT rcv_month, SUM(rcv_qty) AS total\n"
    "FROM DWD_GOODS_RECEIPT_DTL\n"
    "GROUP BY rcv_month\n"
    "ORDER BY rcv_month DESC\n"
    "FETCH FIRST 12 ROWS ONLY"
)

_DIALECT_HINT = (
    "上一轮生成的 SQL 包含 oracle dialect 不允许的函数/语法，已被守卫拦截。\n"
    "请重新生成 SQL，**只用**目标 dialect 的原生写法，**不要**用以下函数：\n"
    "- LIMIT：用 FETCH FIRST N ROWS ONLY（12c+）或 ROWNUM 子查询（11g）替代"
)

_HYPOTHESIS_JSON = json.dumps(
    [
        {
            "statement": _HYPOTHESIS_WITH_LIMIT["statement"],
            "driver": _HYPOTHESIS_WITH_LIMIT["driver"],
            "verificationSql": _HYPOTHESIS_WITH_LIMIT["verificationSql"],
        }
    ],
    ensure_ascii=False,
)
_REPORT_TEXT = "近 12 个月趋势下降。"


# ---- fakes ------------------------------------------------------------------


class _FirstThenOkRunner:
    """``runVerification`` 第一次返回方言违规，第二次返回成功。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.dialectArgsSeen: list[Any] = []

    async def runVerification(self, session, hypothesis, *, adapter=None, dialectArgs=None):
        self.calls.append(hypothesis.verificationSql)
        self.dialectArgsSeen.append(dialectArgs)
        if len(self.calls) == 1:
            return {"rows": [], "error": f"SQL 方言违规: {_DIALECT_HINT}"}
        return {"rows": [{"rcv_month": "2026-09", "total": 100}], "error": None}


class _AlwaysOkRunner:
    """``runVerification`` 永远成功（用于验证"无违规时不调 rewrite"）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def runVerification(self, session, hypothesis, *, adapter=None, dialectArgs=None):
        self.calls.append(hypothesis.verificationSql)
        return {"rows": [{"ok": 1}], "error": None}


class _RewriteScriptedLlm:
    """``complete`` 返回内容由调用序切换：

    - 任何"非 rewrite"调用（第 1/2 次）→ hypothesis JSON 或 report 文本
    - rewrite 调用（用户消息含 hint）→ 喂回重写 SQL

    检测 rewrite 调用靠"user message 包含方言违规前缀 'SQL 方言违规:' 的 hint 字符串"，
    不靠调用计数（_stageVerify 单测绕开 hypothesis/report 相位，调用序可能从 0 开始）。
    """

    def __init__(self, hypothesisJson: str, reportText: str, rewriteResponse: str) -> None:
        self._hypothesisJson = hypothesisJson
        self._reportText = reportText
        self._rewriteResponse = rewriteResponse
        self.calls = 0
        self.rewriteCallMessages: list[Any] = []

    async def complete(self, messages, **kwargs):
        self.calls += 1
        # detect rewrite：user message 包含方言违规 hint 字符串
        is_rewrite = any(
            "oracle dialect 不允许" in (m.content or "") for m in messages
        )
        if is_rewrite:
            self.rewriteCallMessages.append(messages)
            return _FakeResponse(self._rewriteResponse, 80, 80)
        content = self._hypothesisJson if self.calls == 1 else self._reportText
        return _FakeResponse(content, 120, 40)


class _FakeResponse:
    def __init__(self, content: str, promptTokens: int, completionTokens: int) -> None:
        self.content = content
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.modelName = "fake-rewriter"


class _FakeStatic:
    """depsForSession 之外的 resolveClient 用：只包裹 scripted，不动调用计数。"""

    def __init__(self, scripted: _RewriteScriptedLlm) -> None:
        self._scripted = scripted

    async def complete(self, messages, **kwargs):
        return await self._scripted.complete(messages, **kwargs)


def _fakeResolveClient(scripted: _RewriteScriptedLlm):
    """生成 `resolveClient(session, *, state, emit, sessionId) -> (client, config)`。"""

    async def _resolve(session, *, state, emit, sessionId):
        return _FakeStatic(scripted), None

    return _resolve


class _FakeLlmFactory:
    def __init__(self, scripted: _RewriteScriptedLlm) -> None:
        self._scripted = scripted

    def __call__(self, cfg) -> _RewriteScriptedLlm:
        return self._scripted


class _FakeRecordingUsage:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def recordUsage(
        self, session, *, sessionId, purpose, promptTokens, completionTokens, modelName,
        cachedTokens=None,
    ) -> None:
        self.records.append(
            {
                "purpose": purpose,
                "promptTokens": promptTokens,
                "completionTokens": completionTokens,
            }
        )


class _FakeSessionsRepo:
    """``saveFinding`` 记录每次落库的 ``supportingSql`` + supportingData + confidence。"""

    def __init__(self) -> None:
        self.savedFindings: list[dict] = []
        self._seq = 0

    async def saveFinding(
        self, session, *, sessionId, turnId, claimText, supportingSql, supportingData, confidence
    ) -> Any:
        self._seq += 1
        row = type("Row", (), {})()
        row.id = uuid.UUID(int=self._seq)
        self.savedFindings.append(
            {
                "claim": claimText,
                "supportingSql": supportingSql,
                "supportingData": supportingData,
                "confidence": confidence,
            }
        )
        return row


# ---- helpers (复用 test_research_agent_service 的 datasource 模式) ---------


async def _ensureDatasource(dbSession: AsyncSession) -> int:
    """种一个业务数据源行（Oracle 19c），返回 datasource.id。"""
    from sqlalchemy import select

    from app.domain.enums import DataSourceType
    from app.domain.models import DataSource
    from app.infrastructure.security.crypto import encryptApiKey

    existing = await dbSession.scalar(select(DataSource).limit(1))
    if existing is not None:
        return existing.id
    ds = DataSource(
        name="research-rewrite-test-oracle",
        type=DataSourceType.ORACLE,
        host="oracle-test",
        port=1521,
        database_name="THBIDB",
        username="THBI",
        password_encrypted=encryptApiKey("test-password"),
        is_active=True,
        is_default=True,
        oracle_version="19.0.0.0.0",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds.id


async def _createResearchSession(dbSession: AsyncSession, datasourceId: int):
    """种研究会话行（与生产 service 同款 createSession）。"""
    from app.services.research_session_service import ResearchSessionService

    svc = ResearchSessionService()
    return await svc.createSession(
        dbSession, userId=1, question="复盘题", datasourceId=datasourceId
    )


class _StageVerifyHarness(ResearchAgentPhasesMixin):
    """最小化 ``_stageVerify`` 接线 harness。

    只装配本相位用得到的依赖：runner / sessionsRepo / usage / execDeps / llmFactory。
    其它 ``ResearchAgentService`` 的依赖（reporter / planner / chartService / esl
    等）本相位走不到，不必注入。
    """

    def __init__(
        self, runner, llmClient, sessionsRepo, usage, execDeps,
    ) -> None:
        self._runner = runner
        self._sessions = sessionsRepo
        self._usage = usage
        self._exec = execDeps
        self._autoConfirm = True
        self._llmFactory = _FakeLlmFactory(llmClient)
        self._tokenUsage = usage
        self._modelConfigs = None
        self._modelRouter = None
        self._resolveClientFn = _fakeResolveClient(llmClient)

    async def _llmClient(self, session, *, state, emit, sessionId):
        """镜像 ResearchAgentService._llmClient。"""
        client, _ = await self._resolveClientFn(session, state=state, emit=emit, sessionId=sessionId)
        return client

    async def _resolveClient(self, session, *, state, emit, sessionId):
        return await self._resolveClientFn(session, state=state, emit=emit, sessionId=sessionId)

    async def _recordUsage(
        self, session, *, sessionId, purpose, promptTokens, completionTokens, modelName,
        cachedTokens=None,
    ) -> None:
        """镜像 ResearchAgentService._recordUsage（直接调 usageRecorder.recordUsage）。"""
        await self._usage.recordUsage(
            session, sessionId=sessionId, purpose=purpose,
            promptTokens=promptTokens, completionTokens=completionTokens,
            modelName=modelName, cachedTokens=cachedTokens,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test__stageVerify_拦截后_重写_一次_用_重写后_SQL_落库(dbSession: AsyncSession) -> None:
    """happy path：守卫拦到 LIMIT → LLM 单次重写 → 重跑通过 → 落库 rewritten SQL。"""
    runner = _FirstThenOkRunner()
    # 注意：LLM 返回必须用 ```sql ... ``` 围栏包住；rewriter 要求 fence 存在
    scripted = _RewriteScriptedLlm(
        _HYPOTHESIS_JSON, _REPORT_TEXT,
        f"```sql\n{_REWRITTEN_FETCH_FIRST}\n```",
    )
    sessionsRepo = _FakeSessionsRepo()
    usage = _FakeRecordingUsage()
    dsId = await _ensureDatasource(dbSession)
    s = await _createResearchSession(dbSession, dsId)

    execDeps = buildExecutionDeps(
        runner=None,  # 不走 deps.runner；_stageVerify 直接用 self._runner
        chart=None,
        ontology=None,
        nl2sql=None,
        resolveClient=_fakeResolveClient(scripted),
        recordUsage=usage.recordUsage,
    )
    harness = _StageVerifyHarness(runner, scripted, sessionsRepo, usage, execDeps)
    state = {
        "hypotheses": [_HYPOTHESIS_WITH_LIMIT],
        "stepResults": [],
        "choice": {"selectedIndexes": [0]},
        "esl": None,
        "modelId": None,
    }

    pause = await harness._stageVerify(
        dbSession,
        sessionId=s.id,
        turnId=uuid.uuid4(),
        emit=None,
        state=state,
    )
    assert pause is None

    # runner 被调两次：第一次用原 LIMIT SQL（被守卫拦），第二次用重写 SQL 通过
    # 注：state["hypotheses"][0]["verificationSql"] 已被原地改写为 _REWRITTEN_FETCH_FIRST，
    # 所以用局部原值断言，不引用 _HYPOTHESIS_WITH_LIMIT（同一引用已被改）。
    original_sql = (
        "SELECT rcv_month, SUM(rcv_qty) AS total "
        "FROM DWD_GOODS_RECEIPT_DTL "
        "GROUP BY rcv_month ORDER BY rcv_month DESC LIMIT 12"
    )
    assert runner.calls == [original_sql, _REWRITTEN_FETCH_FIRST]
    # state 中 candidate["verificationSql"] 被原地更新为重写后版本
    assert state["hypotheses"][0]["verificationSql"] == _REWRITTEN_FETCH_FIRST
    # saveFinding 落库的 supportingSql 是重写后版本
    assert len(sessionsRepo.savedFindings) == 1
    saved = sessionsRepo.savedFindings[0]
    assert saved["supportingSql"] == _REWRITTEN_FETCH_FIRST
    assert "FETCH FIRST" in saved["supportingSql"]
    assert "LIMIT" not in saved["supportingSql"].upper()
    # supporting_data 应该是非空行（rewrite 后通过，rows=[{rcv_month, total}]）
    assert saved["supportingData"] and "rows" in saved["supportingData"]
    assert saved["supportingData"]["error"] is None
    # confidence 应为候选分 × 0.9（verify OK 因子）；默认候选分 0.5 ⇒ 0.45
    assert saved["confidence"] >= 0.4
    assert saved["confidence"] <= 0.5
    # LLM 第 3 次调用确实是 rewrite（system prompt 提 oracle，user 提 hint）
    assert len(scripted.rewriteCallMessages) == 1
    sys_msg = scripted.rewriteCallMessages[0][0].content.lower()
    assert "oracle" in sys_msg
    user_msg = scripted.rewriteCallMessages[0][1].content
    assert _DIALECT_HINT in user_msg
    assert "ORDER BY rcv_month DESC LIMIT 12" in user_msg  # 原 LIMIT SQL 进了 user prompt


@pytest.mark.asyncio
async def test__stageVerify_重写_失败_仍_用_原_error_落库(dbSession: AsyncSession) -> None:
    """失败路径：rewrite 返 None（LLM 拒答 / fence 缺失 / LLM 抛异常）
    → 仍走第一次的 dialect 违规 error 落库 → confidence = 候选分 × 0.3。
    """
    runner = _FirstThenOkRunner()
    # 假 LLM：第 3 次调用返空（rewriter 返 None）
    scripted = _RewriteScriptedLlm(_HYPOTHESIS_JSON, _REPORT_TEXT, "")
    sessionsRepo = _FakeSessionsRepo()
    usage = _FakeRecordingUsage()
    dsId = await _ensureDatasource(dbSession)
    s = await _createResearchSession(dbSession, dsId)

    execDeps = buildExecutionDeps(
        runner=None,  # 不走 deps.runner；_stageVerify 直接用 self._runner
        chart=None,
        ontology=None,
        nl2sql=None,
        resolveClient=_fakeResolveClient(scripted),
        recordUsage=usage.recordUsage,
    )
    harness = _StageVerifyHarness(runner, scripted, sessionsRepo, usage, execDeps)
    state = {
        "hypotheses": [_HYPOTHESIS_WITH_LIMIT],
        "stepResults": [],
        "choice": {"selectedIndexes": [0]},
        "esl": None,
        "modelId": None,
    }

    pause = await harness._stageVerify(
        dbSession, sessionId=s.id, turnId=uuid.uuid4(),
        emit=None, state=state,
    )
    assert pause is None

    # runner 只被调一次（rewrite 失败 = 不重试）
    assert len(runner.calls) == 1
    # 仍用第一次的 dialect 违规 error 落库
    assert len(sessionsRepo.savedFindings) == 1
    saved = sessionsRepo.savedFindings[0]
    error_text = json.dumps(saved["supportingData"], ensure_ascii=False)
    assert "SQL 方言违规" in error_text
    assert "FETCH FIRST" in error_text or "ROWNUM" in error_text
    # confidence = 候选分 × 0.3（verify fail 因子）
    assert saved["confidence"] <= 0.4
    # rewrite **被尝试**了（不是直接放弃）— LLM 调了 1 次返空，rewriter 返 None
    assert len(scripted.rewriteCallMessages) == 1


@pytest.mark.asyncio
async def test__stageVerify_无_dialect_违规_不_调_rewrite(dbSession: AsyncSession) -> None:
    """happy path：第一次就通过 → 不调 LLM rewrite → runner 只跑一次。
    防"无脑重试"反模式（每次都跑两次 LLM 调用）。
    """
    runner = _AlwaysOkRunner()
    scripted = _RewriteScriptedLlm(_HYPOTHESIS_JSON, _REPORT_TEXT, "SELECT 1 FROM dual")
    sessionsRepo = _FakeSessionsRepo()
    usage = _FakeRecordingUsage()
    dsId = await _ensureDatasource(dbSession)
    s = await _createResearchSession(dbSession, dsId)

    execDeps = buildExecutionDeps(
        runner=None,  # 不走 deps.runner；_stageVerify 直接用 self._runner
        chart=None,
        ontology=None,
        nl2sql=None,
        resolveClient=_fakeResolveClient(scripted),
        recordUsage=usage.recordUsage,
    )
    harness = _StageVerifyHarness(runner, scripted, sessionsRepo, usage, execDeps)
    state = {
        "hypotheses": [_HYPOTHESIS_WITH_LIMIT],
        "stepResults": [],
        "choice": {"selectedIndexes": [0]},
        "esl": None,
        "modelId": None,
    }

    await harness._stageVerify(
        dbSession, sessionId=s.id, turnId=uuid.uuid4(),
        emit=None, state=state,
    )

    assert len(runner.calls) == 1
    # _stageVerify 不应触发任何 LLM 调用（无错误 = 不调 rewrite）
    assert scripted.calls == 0
    assert scripted.rewriteCallMessages == []
