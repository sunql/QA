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


async def test_l1_feature_calc_path_records_evidence(
    client, dbSession: AsyncSession, monkeypatch
):
    """R1 回归：L1 FeatureCalc 路径（_executeCalculationLogic）真执行并落 evidence。

    背景：3fc0a15（feat(chat): L1 KPI semantic match routing）起缺 await，
    coroutine 从未被 await——查询本身与 B2 evidence 钩子双双失效。本用例
    钉住「await 后真正执行 SQL + evidence 落库 + hash 一致」。
    """
    from app.domain.models import (
        BusinessObject,
        DataSource,
        FeatureDefinition,
        KpiCatalog,
    )
    from app.services.chat_service import ChatService

    sqlText = "SELECT 42 AS total"

    # Arrange：business_object / data_source / feature_definition / kpi_catalog
    dbSession.add(BusinessObject(code="BO_HOOK", name="钩子测试对象"))
    ds = DataSource(
        name="evidence-hook-self",
        type="POSTGRESQL",
        host="localhost",
        port=5434,
        database_name="qa_metadata_test",
        username="qa_user",
        password_encrypted="not-used",  # _adapterProvider 被替换，永不解密
    )
    dbSession.add(ds)
    await dbSession.flush()
    dbSession.add(
        FeatureDefinition(
            feature_name="l1-hook-feature",
            entity_type="BO_HOOK",
            feature_definition=sqlText,  # service 用 feature_definition == kpi.formula 关联
            calculation_logic=sqlText,
            datasource_id=ds.id,
            is_enabled=True,
        )
    )
    dbSession.add(KpiCatalog(kpi_code="L1_HOOK", kpi_name="L1钩子指标", formula=sqlText))
    await dbSession.commit()

    kpi = (
        await dbSession.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code == "L1_HOOK")
        )
    ).scalar_one()

    adapter = _makeAdapter()
    service = ChatService()
    # _adapterProvider 定向到测试库自身 adapter（绕开数据源真实连接解密）
    monkeypatch.setattr(service, "_adapterProvider", lambda datasourceId, ds_: adapter)
    try:
        data = await service._executeCalculationLogic(kpi, "L1_HOOK", dbSession)
    finally:
        await adapter.dispose()
    await _drainPendingEvidenceTasks()

    # Act 断言：查询真正执行（不再是 [{"value": <coroutine>}]）
    assert data == [{"total": 42}]
    # evidence 断言：B2 钩子在 L1 路径同样触发
    evidences = await _loadSqlQueryEvidences(dbSession)
    assert len(evidences) == 1
    ev = evidences[0]
    assert ev.payload["sql"] == sqlText
    assert ev.payload["row_count"] == 1
    expectedHash = hashlib.sha256(
        json.dumps([{"total": 42}], sort_keys=True, default=str).encode()
    ).hexdigest()
    assert ev.payload["result_hash"] == expectedHash
