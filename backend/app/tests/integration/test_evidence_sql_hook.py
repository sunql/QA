"""Integration tests for execute_read_only → SQL_QUERY evidence 自动落库（任务 B2）。

真实 PG（qa_metadata_test）双端验证：
- 业务端：_SqlaAdapter 直连测试库自身执行只读 SELECT（触发装饰器钩子）
- 证据端：getSessionFactory（被 client fixture 换到测试库）落 evidence 行

契约（brief + coordinator 拍板）：
- 执行成功 → evidence 表出现一条 source_type=SQL_QUERY 行，result_hash 与
  手工计算 sha256(json.dumps(rows, sort_keys=True, default=str)) 一致
- claim_id 为 NULL（蓝图 §5.7：claim 关联由上层填充）；0098 已放开非空约束
- contextvar 带 session_id 的路径 → 行带 session_id；非 chat 路径 → NULL
- evidence 落库失败 → 查询照常返回 + 日志 warning（best-effort）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import Evidence
from app.infrastructure.business_db_pool import _SqlaAdapter
from app.services import evidence_record_service as ers
from app.tests._pg_support import resolveTestDatabaseUrl

pytestmark = pytest.mark.integration


async def _drainPendingEvidenceTasks() -> None:
    """等后台落库任务跑完（create_task 弱引用防 GC 集合，同 ontology 测试套路）。"""
    while ers._PENDING_EVIDENCE_TASKS:
        remaining = list(ers._PENDING_EVIDENCE_TASKS)
        await asyncio.gather(*remaining, return_exceptions=True)


def _makeAdapter() -> _SqlaAdapter:
    """业务端 adapter：指向测试 PG 自身（execute_read_only 的 SQL Guard 全链路生效）。"""
    adapter = _SqlaAdapter(resolveTestDatabaseUrl())
    adapter.datasourceId = 7
    return adapter


async def _loadSqlQueryEvidences(dbSession: AsyncSession) -> list:
    result = await dbSession.execute(
        select(Evidence).where(Evidence.source_type == "SQL_QUERY")
    )
    return list(result.scalars().all())


async def test_execute_read_only_records_sql_query_evidence(
    client, dbSession: AsyncSession
):
    adapter = _makeAdapter()
    token = ers.setChatSessionId("chat-e2e-1")
    try:
        rows = await adapter.execute_read_only("SELECT 1 AS v")
    finally:
        ers.resetChatSessionId(token)
    await adapter.dispose()

    assert rows == [{"v": 1}]
    await _drainPendingEvidenceTasks()

    evidences = await _loadSqlQueryEvidences(dbSession)
    assert len(evidences) == 1
    ev = evidences[0]
    assert ev.claim_id is None
    assert ev.session_id == "chat-e2e-1"
    assert ev.payload["sql"] == "SELECT 1 AS v"
    assert ev.payload["params"] == {}
    assert ev.payload["row_count"] == 1
    assert ev.payload["datasource_id"] == 7
    assert ev.payload["execution_time_ms"] >= 0
    expectedHash = hashlib.sha256(
        json.dumps([{"v": 1}], sort_keys=True, default=str).encode()
    ).hexdigest()
    assert ev.payload["result_hash"] == expectedHash


async def test_execute_without_chat_context_records_null_session(
    client, dbSession: AsyncSession
):
    """非 chat 调用方（无 contextvar）默认空 session_id，不阻塞。"""
    adapter = _makeAdapter()
    rows = await adapter.execute_read_only("SELECT 2 AS v")
    await adapter.dispose()

    assert rows == [{"v": 2}]
    await _drainPendingEvidenceTasks()

    evidences = await _loadSqlQueryEvidences(dbSession)
    assert len(evidences) == 1
    assert evidences[0].session_id is None


async def test_query_succeeds_when_evidence_persist_fails(
    client, dbSession: AsyncSession, monkeypatch, caplog
):
    """best-effort 验证：evidence 落库炸了，查询结果照常返回 + warning 日志。"""
    async def boom(*, payload, sessionId):
        raise RuntimeError("evidence db down")

    monkeypatch.setattr(ers, "persistSqlQueryEvidence", boom)
    adapter = _makeAdapter()
    try:
        with caplog.at_level(logging.WARNING):
            rows = await adapter.execute_read_only("SELECT 3 AS v")
    finally:
        await adapter.dispose()
    await _drainPendingEvidenceTasks()

    assert rows == [{"v": 3}]
    warningMessages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("evidence" in m.lower() for m in warningMessages), (
        f"未找到 evidence 落库失败 warning：{warningMessages}"
    )
