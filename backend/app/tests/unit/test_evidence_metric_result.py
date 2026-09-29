"""Unit tests for METRIC_RESULT evidence（v3.1 任务 MR，蓝图 §4.12）。

镜像 test_evidence_record_service.py（B2）模式：纯逻辑 + 假替身，不碰真实 PG：
- extractSampleValue：首行首个数值单元格（int/float/Decimal/数值串；bool 跳过；
  无数值/空行/异常 → None 不抛）
- buildMetricResultPayload：九字段契约（四要素 metric_code/period/sample_value/
  calc_time + 追溯上下文）
- scheduleMetricResultEvidence：后台任务落库 + 失败仅 warning（best-effort，
  镜像 scheduleSqlQueryEvidence 语义）
- _persistBestEffort 复用：persist 参数路由 + 默认仍走 SQL_QUERY
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import pytest

from app.services import evidence_record_service as ers


async def _drainPendingEvidenceTasks() -> None:
    """等待模块级 _PENDING_EVIDENCE_TASKS 中所有后台任务完成（同 B2 单测套路）。"""
    while ers._PENDING_EVIDENCE_TASKS:
        remaining = list(ers._PENDING_EVIDENCE_TASKS)
        await asyncio.gather(*remaining)


# ---------------------------------------------------------------------------
# extractSampleValue（纯函数：首行首个数值单元格）
# ---------------------------------------------------------------------------


def test_extract_sample_value_takes_first_numeric_cell():
    assert ers.extractSampleValue([{"name": "OTD", "v": 3}]) == 3.0


def test_extract_sample_value_skips_text_before_number():
    assert ers.extractSampleValue([{"name": "x", "amount": 12.5}]) == 12.5


def test_extract_sample_value_handles_decimal_via_str():
    # memory 教训：Decimal/float 边界——先 str 再 float，避免 JSONB 序列化差异
    assert ers.extractSampleValue([{"v": Decimal("9.30")}]) == 9.3


def test_extract_sample_value_converts_numeric_string():
    assert ers.extractSampleValue([{"v": "42.5"}]) == 42.5


def test_extract_sample_value_skips_bool():
    # bool 是 int 子类，语义上不是数值单元格
    assert ers.extractSampleValue([{"flag": True, "v": 2}]) == 2.0


def test_extract_sample_value_returns_none_when_no_numeric():
    assert ers.extractSampleValue([{"name": "x"}]) is None


def test_extract_sample_value_returns_none_for_empty_rows():
    assert ers.extractSampleValue([]) is None


def test_extract_sample_value_returns_none_for_empty_first_row():
    assert ers.extractSampleValue([{}, {"v": 1}]) is None


def test_extract_sample_value_never_raises_on_unconvertible():
    assert ers.extractSampleValue([{"v": object()}]) is None


def test_extract_sample_value_does_not_mutate_rows():
    rows = [{"name": "x", "v": Decimal("1.5")}]
    ers.extractSampleValue(rows)
    assert rows == [{"name": "x", "v": Decimal("1.5")}]


# ---------------------------------------------------------------------------
# buildMetricResultPayload（九字段契约，蓝图 §4.12）
# ---------------------------------------------------------------------------


def test_build_metric_result_payload_full_contract():
    payload = ers.buildMetricResultPayload(
        metricCode="KPI_SUPPLIER_OTD",
        metricName="供应商准时率",
        confidence=0.95,
        period=None,
        sampleValue=0.97,
        rowCount=1,
        resultHash="h" * 8,
        calcTime="2026-09-29T00:00:00+00:00",
        datasourceId=7,
    )
    assert payload == {
        "metric_code": "KPI_SUPPLIER_OTD",
        "metric_name": "供应商准时率",
        "confidence": 0.95,
        "period": None,
        "sample_value": 0.97,
        "row_count": 1,
        "result_hash": "h" * 8,
        "calc_time": "2026-09-29T00:00:00+00:00",
        "datasource_id": 7,
    }


def test_build_metric_result_payload_allows_none_fields():
    payload = ers.buildMetricResultPayload(
        metricCode="KPI_X",
        metricName=None,
        confidence=1.0,
        period=None,
        sampleValue=None,
        rowCount=0,
        resultHash="h",
        calcTime="2026-09-29T00:00:00+00:00",
        datasourceId=None,
    )
    assert payload["metric_name"] is None
    assert payload["sample_value"] is None
    assert payload["datasource_id"] is None


# ---------------------------------------------------------------------------
# scheduleMetricResultEvidence（假 session factory，不碰真实 DB）
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


async def test_schedule_persists_metric_result_evidence_row(evidenceSink):
    rows = [{"total": 42}]
    ers.scheduleMetricResultEvidence(
        metricCode="KPI_HOOK",
        metricName="钩子指标",
        confidence=0.9,
        rows=rows,
        datasourceId=5,
    )
    await _drainPendingEvidenceTasks()

    assert len(evidenceSink) == 1
    ev = evidenceSink[0]
    assert ev.source_type == "METRIC_RESULT"
    # 蓝图 §5.7：证据创建时无 claim，关联由上层填充
    assert ev.claim_id is None
    assert ev.session_id is None
    payload = ev.payload
    # 蓝图 §4.12 四要素 + 追溯上下文
    assert payload["metric_code"] == "KPI_HOOK"
    assert payload["metric_name"] == "钩子指标"
    assert payload["confidence"] == 0.9
    assert payload["period"] is None  # 执行时上下文无 period，不猜
    assert payload["sample_value"] == 42.0
    assert payload["row_count"] == 1
    assert payload["result_hash"] == ers.computeResultHash(rows)
    assert isinstance(payload["calc_time"], str)
    assert payload["calc_time"].endswith("+00:00")  # UTC ISO8601
    assert payload["datasource_id"] == 5


async def test_schedule_metric_captures_contextvar_session_id(evidenceSink):
    token = ers.setChatSessionId("chat-mr-9")
    try:
        ers.scheduleMetricResultEvidence(
            metricCode="KPI_HOOK",
            metricName=None,
            confidence=1.0,
            rows=[{"v": 1}],
            datasourceId=None,
        )
    finally:
        ers.resetChatSessionId(token)
    await _drainPendingEvidenceTasks()
    assert evidenceSink[0].session_id == "chat-mr-9"


async def test_schedule_metric_swallows_persist_failure_with_warning(
    evidenceSink, monkeypatch, caplog
):
    async def boom(*, payload, sessionId):
        raise RuntimeError("evidence db down")

    monkeypatch.setattr(ers, "persistMetricResultEvidence", boom)
    with caplog.at_level(
        logging.WARNING, logger="app.services.evidence_record_service"
    ):
        ers.scheduleMetricResultEvidence(
            metricCode="KPI_HOOK",
            metricName=None,
            confidence=1.0,
            rows=[{"v": 1}],
            datasourceId=None,
        )
        await _drainPendingEvidenceTasks()

    assert any("evidence" in r.message.lower() for r in caplog.records)


async def test_schedule_metric_swallows_sample_value_failure(evidenceSink, monkeypatch):
    def badExtract(rows):
        raise TypeError("not iterable")

    monkeypatch.setattr(ers, "extractSampleValue", badExtract)
    ers.scheduleMetricResultEvidence(
        metricCode="KPI_HOOK",
        metricName=None,
        confidence=1.0,
        rows=[{"v": 1}],
        datasourceId=None,
    )
    await _drainPendingEvidenceTasks()
    assert evidenceSink == []  # 调度失败不产生半条记录，也不抛出


async def test_schedule_metric_handles_empty_rows(evidenceSink):
    """空结果行不抛：sample_value=None + row_count=0，照常落。"""
    ers.scheduleMetricResultEvidence(
        metricCode="KPI_HOOK",
        metricName=None,
        confidence=1.0,
        rows=[],
        datasourceId=None,
    )
    await _drainPendingEvidenceTasks()
    assert len(evidenceSink) == 1
    assert evidenceSink[0].payload["sample_value"] is None
    assert evidenceSink[0].payload["row_count"] == 0


# ---------------------------------------------------------------------------
# _persistBestEffort 复用（persist 参数路由 + 默认仍走 SQL_QUERY）
# ---------------------------------------------------------------------------


async def test_persist_best_effort_routes_custom_persist(evidenceSink):
    captured: list = []

    async def fakePersist(*, payload, sessionId):
        captured.append((payload, sessionId))

    await ers._persistBestEffort(
        payload={"k": 1}, sessionId="s1", persist=fakePersist
    )
    assert captured == [({"k": 1}, "s1")]


async def test_persist_best_effort_default_targets_sql_query(evidenceSink, monkeypatch):
    calls: list = []

    async def fakeSqlPersist(*, payload, sessionId):
        calls.append(sessionId)

    monkeypatch.setattr(ers, "persistSqlQueryEvidence", fakeSqlPersist)
    await ers._persistBestEffort(payload={"sql": "SELECT 1"}, sessionId="s2")
    assert calls == ["s2"]
