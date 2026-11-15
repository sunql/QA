"""P2 D15：回灌重试那一轮必须与首次生成**用同一份 schema 口径**。

背景：开关 `ENABLE_SEMANTIC_REGISTRY_NL2SQL` 打开后，schema 文本由
「语义快照闸门」渲染（`app/services/semantic_schema_gate.py`）⇒ 需要 DB 会话
才能取到已发布快照；会话缺席时闸门**确定地直读**治理表（宁可确定，不要
「冷缓存抛异常 / 热缓存命中」的不确定）。

`_runQueryWithRetry` 的回灌重试此前**不传 session** ⇒ 那一轮的 `generateSql`
拿不到会话 ⇒ 闸门确定地直读 ⇒ **首次生成用快照、重试用直读** ⇒
同一轮问答里两次 prompt 的 schema 口径不一致（连类内列序都不同）⇒
重试生成的 SQL 是在**另一份口径**下产出的。

本文件钉死：回灌重试必须把 session 传下去。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.chat_helpers import _SqlOutcome
from app.services.chat_usage import UsageMixin


class _SpyNl2sql:
    """记录回灌重试那一次 `generateSql` 的实参。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generateSql(self, question, classes, llmClient, modelConfig, **kwargs):
        self.calls.append({"question": question, **kwargs})
        raise RuntimeError("回灌重试的生成也失败")


class _Harness:
    """`_runQueryWithRetry` 的最小宿主。

    刻意**不**用真 ChatService：那是 HTTP 层 + 会话 + 用量落库的整套机器。
    本用例要证明的只有一件事：**回灌重试有没有把 session 传下去**。
    """

    def __init__(self, nl2sql) -> None:
        self._nl2sql = nl2sql
        self._llmFactory = lambda cfg: None
        self._consumedTokens = UsageMixin._consumedTokens

    async def _runQuery(self, pc, dto, sql, *, user_id=None):
        raise RuntimeError("首次执行失败（触发回灌重试）")

    async def _runQueryWithRetry(self, session, dto, pc, outcome, **kwargs):
        return await UsageMixin._runQueryWithRetry(
            self, session, dto, pc, outcome, **kwargs
        )


def _pipelineContext() -> SimpleNamespace:
    return SimpleNamespace(
        classes=[],
        joins=[],
        ds=SimpleNamespace(type="oracle", oracle_version="19c", username="APP"),
        contextPrompt=None,
        fewShot=None,
        valueSamples=None,
        driftWarning=None,
        selected=SimpleNamespace(model_name="test-model", id=1),
    )


async def test_回灌重试必须把session传给generateSql() -> None:
    """回灌重试那一轮也要走同一份 schema 口径（否则首次用快照、重试用直读）。"""
    spy = _SpyNl2sql()
    harness = _Harness(spy)
    session = object()
    dto = SimpleNamespace(question="问题", sessionId="s-1")
    outcome = _SqlOutcome(plan=None, sql="SELECT 1 FROM T", sqlConfig=None)

    with pytest.raises(RuntimeError):
        await harness._runQueryWithRetry(session, dto, _pipelineContext(), outcome)

    assert spy.calls, "回灌重试应当发生（首次执行失败 ⇒ 触发一轮重试）"
    assert spy.calls[0].get("session") is session, (
        "回灌重试必须带上 session，否则那一轮拿不到语义快照 ⇒ "
        "首次生成与重试用的是两份不同的 schema 口径"
    )
