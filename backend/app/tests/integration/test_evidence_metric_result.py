"""Integration tests for L1 KPI hit → METRIC_RESULT evidence 自动落库（v3.1 任务 MR）。

真实 PG（qa_metadata_test）验证（镜像 test_evidence_sql_hook.py 的 L1 用例模式）：
- L1 match 命中 + 执行成功 → evidence 行 source_type=METRIC_RESULT 落库，
  payload JSONB 断言蓝图 §4.12 四要素（metric_code / period / sample_value /
  calc_time）+ 追溯上下文
- KPI 未命中（match=None）→ 不落
- 执行失败 → 不落

契约：claim_id 恒 NULL（蓝图 §5.7 上层填充）；period L1 上下文无语义恒 None
（不猜）；调度/落库任何失败只 warning 不影响查询主链路（best-effort）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import Evidence
from app.infrastructure.business_db_pool import _SqlaAdapter
from app.services import evidence_record_service as ers
from app.services.kpi_semantic_match_service import KpiMatchResult
from app.tests._pg_support import resolveTestDatabaseUrl

pytestmark = pytest.mark.integration


async def _drainPendingEvidenceTasks() -> None:
    """等后台落库任务跑完（create_task 弱引用防 GC 集合，同 B2 集成套路）。"""
    while ers._PENDING_EVIDENCE_TASKS:
        remaining = list(ers._PENDING_EVIDENCE_TASKS)
        await asyncio.gather(*remaining, return_exceptions=True)


def _makeAdapter() -> _SqlaAdapter:
    """业务端 adapter：指向测试 PG 自身（execute_read_only 的 SQL Guard 全链路生效）。"""
    adapter = _SqlaAdapter(resolveTestDatabaseUrl())
    adapter.datasourceId = 7
    return adapter


async def _loadMetricResultEvidences(dbSession: AsyncSession) -> list:
    result = await dbSession.execute(
        select(Evidence).where(Evidence.source_type == "METRIC_RESULT")
    )
    return list(result.scalars().all())


async def _seedL1HookKpi(dbSession: AsyncSession, *, kpiCode: str):
    """Arrange：business_object / data_source / feature_definition / kpi_catalog。

    与 test_evidence_sql_hook.py 的 L1 用例同款 seed（每测试 TRUNCATE 隔离）。
    返回 (kpi, dataSourceId)。
    """
    from app.domain.models import (
        BusinessObject,
        DataSource,
        FeatureDefinition,
        KpiCatalog,
    )

    sqlText = "SELECT 42 AS total"

    dbSession.add(BusinessObject(code="BO_MR", name="MR 测试对象"))
    ds = DataSource(
        name="metric-result-self",
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
            feature_name="mr-hook-feature",
            entity_type="BO_MR",
            feature_definition=sqlText,  # service 用 feature_definition == kpi.formula 关联
            calculation_logic=sqlText,
            datasource_id=ds.id,
            is_enabled=True,
        )
    )
    dbSession.add(KpiCatalog(kpi_code=kpiCode, kpi_name="MR钩子指标", formula=sqlText))
    await dbSession.commit()

    from app.domain.models import KpiCatalog as KpiCatalogModel

    kpi = (
        await dbSession.execute(
            select(KpiCatalogModel).where(KpiCatalogModel.kpi_code == kpiCode)
        )
    ).scalar_one()
    return kpi, ds.id


async def test_l1_match_and_success_records_metric_result(
    client, dbSession: AsyncSession, monkeypatch
):
    """L1 命中 + 执行成功 → METRIC_RESULT 行落库，payload 四要素齐全。"""
    from app.services.chat_service import ChatService

    kpi, datasourceId = await _seedL1HookKpi(dbSession, kpiCode="MR_HOOK")

    adapter = _makeAdapter()
    service = ChatService()
    monkeypatch.setattr(service, "_adapterProvider", lambda datasourceId, ds_: adapter)
    match = KpiMatchResult(code="MR_HOOK", confidence=0.95)
    token = ers.setChatSessionId("chat-mr-1")
    try:
        data = await service._executeCalculationLogic(
            kpi, "MR_HOOK", dbSession, match=match
        )
    finally:
        ers.resetChatSessionId(token)
        await adapter.dispose()
    await _drainPendingEvidenceTasks()

    # 执行成功（rows 已返回）
    assert data == [{"total": 42}]

    evidences = await _loadMetricResultEvidences(dbSession)
    assert len(evidences) == 1
    ev = evidences[0]
    assert ev.claim_id is None
    assert ev.session_id == "chat-mr-1"
    payload = ev.payload
    # 蓝图 §4.12 四要素
    assert payload["metric_code"] == "MR_HOOK"
    assert payload["period"] is None  # L1 上下文无 period 语义，不猜
    assert payload["sample_value"] == 42.0  # 首行首个数值单元格
    assert isinstance(payload["calc_time"], str)
    assert payload["calc_time"].endswith("+00:00")  # UTC ISO8601
    # 追溯上下文
    assert payload["metric_name"] == "MR钩子指标"
    assert payload["confidence"] == 0.95
    assert payload["row_count"] == 1
    assert payload["datasource_id"] == datasourceId
    expectedHash = hashlib.sha256(
        json.dumps([{"total": 42}], sort_keys=True, default=str).encode()
    ).hexdigest()
    assert payload["result_hash"] == expectedHash


async def test_no_match_records_no_metric_result(
    client, dbSession: AsyncSession, monkeypatch
):
    """match=None（KPI 未命中 / 直调路径）→ 执行成功也不落 METRIC_RESULT。"""
    from app.services.chat_service import ChatService

    kpi, _ = await _seedL1HookKpi(dbSession, kpiCode="MR_NOMATCH")

    adapter = _makeAdapter()
    service = ChatService()
    monkeypatch.setattr(service, "_adapterProvider", lambda datasourceId, ds_: adapter)
    try:
        data = await service._executeCalculationLogic(kpi, "MR_NOMATCH", dbSession)
    finally:
        await adapter.dispose()
    await _drainPendingEvidenceTasks()

    assert data == [{"total": 42}]  # 查询本身照常成功
    assert await _loadMetricResultEvidences(dbSession) == []


async def test_execution_failure_records_no_metric_result(
    client, dbSession: AsyncSession, monkeypatch
):
    """L1 命中但执行失败 → 不落（接线在执行成功分支，不在 match 处）。"""
    from app.services.chat_service import ChatService

    kpi, _ = await _seedL1HookKpi(dbSession, kpiCode="MR_FAIL")

    adapter = _makeAdapter()
    service = ChatService()
    monkeypatch.setattr(service, "_adapterProvider", lambda datasourceId, ds_: adapter)

    async def boom(sql: str):
        raise RuntimeError("db down")

    monkeypatch.setattr(adapter, "execute_read_only", boom)
    match = KpiMatchResult(code="MR_FAIL", confidence=0.95)
    data = await service._executeCalculationLogic(
        kpi, "MR_FAIL", dbSession, match=match
    )
    await _drainPendingEvidenceTasks()

    assert data is None  # 执行失败 → None
    assert await _loadMetricResultEvidences(dbSession) == []
