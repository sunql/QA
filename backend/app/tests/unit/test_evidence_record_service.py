"""Unit tests for evidence_record_service（v3.1 任务 B2）。

纯逻辑 + 假替身，不碰真实 PG：
- result_hash 口径 = brief 原文 sha256(json.dumps(rows, sort_keys=True, default=str))
- ContextVar set/reset 语义
- payload 六字段契约（sql/params/result_hash/row_count/execution_time_ms/datasource_id）
- scheduleSqlQueryEvidence：后台任务落库 + 失败仅 warning（best-effort）
- business_db_pool 装饰器：成功后调度一次、查询失败不调度且异常照常抛出
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

import pytest

from app.services import evidence_record_service as ers


async def _drainPendingEvidenceTasks() -> None:
    """等待模块级 _PENDING_EVIDENCE_TASKS 中所有后台任务完成（同 ontology 测试套路）。"""
    while ers._PENDING_EVIDENCE_TASKS:
        remaining = list(ers._PENDING_EVIDENCE_TASKS)
        await asyncio.gather(*remaining)


# ---------------------------------------------------------------------------
# result_hash
# ---------------------------------------------------------------------------


def test_compute_result_hash_matches_brief_formula():
    rows = [{"a": 1, "b": "x"}]
    expected = hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode()
    ).hexdigest()
    assert ers.computeResultHash(rows) == expected


def test_compute_result_hash_is_key_order_insensitive():
    assert ers.computeResultHash([{"a": 1, "b": 2}]) == ers.computeResultHash(
        [{"b": 2, "a": 1}]
    )


def test_compute_result_hash_handles_non_json_types_via_str():
    rows = [{"when": "2026-01-01"}]
    assert len(ers.computeResultHash(rows)) == 64


# ---------------------------------------------------------------------------
# ContextVar
# ---------------------------------------------------------------------------


def test_chat_session_contextvar_roundtrip():
    token = ers.setChatSessionId("chat-1")
    try:
        assert ers.currentChatSessionId() == "chat-1"
    finally:
        ers.resetChatSessionId(token)
    assert ers.currentChatSessionId() is None


def test_chat_session_contextvar_default_none():
    assert ers.currentChatSessionId() is None


# ---------------------------------------------------------------------------
# payload 六字段契约
# ---------------------------------------------------------------------------


def test_build_sql_evidence_payload_six_fields():
    payload = ers.buildSqlEvidencePayload(
        sql="SELECT 1",
        resultHash="h",
        rowCount=3,
        executionTimeMs=12,
        datasourceId=7,
    )
    assert payload == {
        "sql": "SELECT 1",
        "params": {},
        "result_hash": "h",
        "row_count": 3,
        "execution_time_ms": 12,
        "datasource_id": 7,
    }


def test_build_sql_evidence_payload_allows_null_datasource():
    payload = ers.buildSqlEvidencePayload(
        sql="SELECT 1",
        resultHash="h",
        rowCount=0,
        executionTimeMs=0,
        datasourceId=None,
    )
    assert payload["datasource_id"] is None


# ---------------------------------------------------------------------------
# scheduleSqlQueryEvidence（假 session factory，不碰真实 DB）
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, sink: list):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *excInfo):
        return None

    def add(self, obj):
        self._sink.append(obj)

    async def commit(self):
        pass


@pytest.fixture()
def evidenceSink(monkeypatch):
    """把 getSessionFactory 换成假工厂，收集 add 进来的 Evidence 对象。"""
    sink: list = []
    monkeypatch.setattr(
        ers, "getSessionFactory", lambda: (lambda: _FakeSession(sink))
    )
    return sink


async def test_schedule_persists_sql_query_evidence_row(evidenceSink):
    ers.scheduleSqlQueryEvidence(
        sql="SELECT 1 AS v",
        rows=[{"v": 1}],
        startedAt=time.monotonic(),
        datasourceId=5,
    )
    await _drainPendingEvidenceTasks()

    assert len(evidenceSink) == 1
    ev = evidenceSink[0]
    assert ev.source_type == "SQL_QUERY"
    # 蓝图 §5.7：SQL_QUERY 证据创建时无 claim，关联由上层填充
    assert ev.claim_id is None
    assert ev.session_id is None
    assert ev.payload["sql"] == "SELECT 1 AS v"
    assert ev.payload["result_hash"] == ers.computeResultHash([{"v": 1}])
    assert ev.payload["row_count"] == 1
    assert ev.payload["datasource_id"] == 5
    assert ev.payload["execution_time_ms"] >= 0


async def test_schedule_captures_contextvar_session_id(evidenceSink):
    token = ers.setChatSessionId("chat-9")
    try:
        ers.scheduleSqlQueryEvidence(
            sql="SELECT 1",
            rows=[],
            startedAt=time.monotonic(),
            datasourceId=None,
        )
    finally:
        ers.resetChatSessionId(token)
    await _drainPendingEvidenceTasks()
    assert evidenceSink[0].session_id == "chat-9"


async def test_schedule_swallows_persist_failure_with_warning(
    evidenceSink, monkeypatch, caplog
):
    async def boom(*, payload, sessionId):
        raise RuntimeError("evidence db down")

    monkeypatch.setattr(ers, "persistSqlQueryEvidence", boom)
    with caplog.at_level(
        logging.WARNING, logger="app.services.evidence_record_service"
    ):
        ers.scheduleSqlQueryEvidence(
            sql="SELECT 1",
            rows=[{"v": 1}],
            startedAt=time.monotonic(),
            datasourceId=None,
        )
        await _drainPendingEvidenceTasks()

    assert any("evidence" in r.message.lower() for r in caplog.records)


async def test_schedule_swallows_hash_failure(evidenceSink, monkeypatch):
    def badHash(rows):
        raise TypeError("not serializable")

    monkeypatch.setattr(ers, "computeResultHash", badHash)
    ers.scheduleSqlQueryEvidence(
        sql="SELECT 1",
        rows=[{"v": 1}],
        startedAt=time.monotonic(),
        datasourceId=None,
    )
    await _drainPendingEvidenceTasks()
    assert evidenceSink == []  # 调度失败不产生半条记录，也不抛出


# ---------------------------------------------------------------------------
# business_db_pool 装饰器（假 adapter，不碰真实 DB）
# ---------------------------------------------------------------------------


from app.infrastructure.business_db_pool import _recordEvidenceAfterSuccess  # noqa: E402


class _DummyAdapter:
    datasourceId = 42

    @_recordEvidenceAfterSuccess
    async def execute_read_only(self, sql: str):
        if sql == "BOOM":
            raise RuntimeError("query failed")
        return [{"v": 1}]


async def test_decorator_schedules_evidence_after_success(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.infrastructure.business_db_pool.scheduleSqlQueryEvidence",
        lambda **kwargs: calls.append(kwargs),
    )
    rows = await _DummyAdapter().execute_read_only("SELECT 1")

    assert rows == [{"v": 1}]
    assert len(calls) == 1
    assert calls[0]["sql"] == "SELECT 1"
    assert calls[0]["rows"] == [{"v": 1}]
    assert calls[0]["datasourceId"] == 42
    assert isinstance(calls[0]["startedAt"], float)


async def test_decorator_does_not_schedule_when_query_fails(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.infrastructure.business_db_pool.scheduleSqlQueryEvidence",
        lambda **kwargs: calls.append(kwargs),
    )
    with pytest.raises(RuntimeError, match="query failed"):
        await _DummyAdapter().execute_read_only("BOOM")

    assert calls == []
