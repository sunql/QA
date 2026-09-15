"""DataQualityEvaluatorDispatcher 单测（Phase 1.2）。

覆盖 dispatcher 自身的调度逻辑：规则缺失 / 数据源缺失 / 不支持的 rule_type /
异常被 catch 后写入 message 字段。evaluator 模块本身走 test_data_quality_evaluators。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.models import DataQualityRule, DataSource
from app.services.data_quality_evaluator import DataQualityEvaluatorDispatcher


class _FakeSession:
    """够 dispatcher 用即可：session.get(id) / session.execute(stmt) 与最小 async context。"""

    def __init__(self, rules: dict[int, Any] | None = None, dss: dict[int, Any] | None = None) -> None:
        self._rules = rules or {}
        self._dss = dss or {}
        self.commits = 0

    async def get(self, cls, key):
        if cls is DataQualityRule:
            return self._rules.get(key)
        if cls is DataSource:
            return self._dss.get(key)
        return None

    async def execute(self, stmt):
        """支持 `select(DataQualityRule).where(...in_(rule_ids))`：把 rules 全量返回。"""
        # 这里只服务于 batch 的 in_ 查询；返回所有 _rules 作为 scalars()
        class _Result:
            def scalars(self_inner):
                class _Scalars:
                    def all(self_inner_inner):
                        return list(self._rules.values())
                return _Scalars()
        return _Result()

    async def commit(self) -> None:
        self.commits += 1


class _StubAdapter:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or [{"total": 10, "passed": 8}]

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        return self.rows


def _rule(
    rid: int = 1,
    *,
    rule_type: str = "COMPLETENESS",
    datasource_id: int = 100,
    threshold: str = "50.00",
) -> Any:
    return SimpleNamespace(
        id=rid,
        rule_code=f"R{rid}",
        rule_type=rule_type,
        datasource_id=datasource_id,
        target_table="T",
        target_column="C",
        rule_expression="C > 0",
        threshold=threshold,
        updated_time=None,
    )


def _ds(dsid: int = 100) -> Any:
    return SimpleNamespace(id=dsid, type="postgresql")


@pytest.mark.asyncio
async def test_evaluate_rule_not_found(monkeypatch) -> None:
    """dispatcher 规则不存在 → NotFoundError。"""
    session = _FakeSession({})
    dispatcher = DataQualityEvaluatorDispatcher()
    with pytest.raises(Exception, match="不存在"):
        await dispatcher.evaluate(session, 999)


@pytest.mark.asyncio
async def test_evaluate_datasource_missing_returns_error_result(monkeypatch) -> None:
    """规则存在但数据源被删 → ERROR + message，不抛异常。"""
    session = _FakeSession(rules={1: _rule()})  # dss 为空 → DataSource 缺失
    dispatcher = DataQualityEvaluatorDispatcher()
    result = await dispatcher.evaluate(session, 1)
    assert result.status == "ERROR"
    assert result.message and "数据源" in result.message


@pytest.mark.asyncio
async def test_evaluate_unsupported_rule_type_returns_error(monkeypatch) -> None:
    """未知 rule_type → ERROR + message，不抛异常。"""
    rule = _rule(rule_type="BOGUS")
    session = _FakeSession(rules={1: rule}, dss={100: _ds()})
    monkeypatch.setattr(
        "app.services.data_quality_evaluator.get_adapter",
        lambda did, ds: _StubAdapter(),
    )
    dispatcher = DataQualityEvaluatorDispatcher()
    result = await dispatcher.evaluate(session, 1)
    assert result.status == "ERROR"
    assert "不支持" in result.message


@pytest.mark.asyncio
async def test_evaluate_exception_in_evaluator_returns_error(monkeypatch) -> None:
    """evaluator 抛异常 → dispatcher 捕获并写入 message。"""
    rule = _rule(rule_type="VALIDITY", datasource_id=100)
    session = _FakeSession(rules={1: rule}, dss={100: _ds()})

    class _BoomAdapter:
        async def execute_read_only(self, sql):
            raise RuntimeError("业务库挂了")

    monkeypatch.setattr(
        "app.services.data_quality_evaluator.get_adapter",
        lambda did, ds: _BoomAdapter(),
    )
    dispatcher = DataQualityEvaluatorDispatcher()
    result = await dispatcher.evaluate(session, 1)
    assert result.status == "ERROR"
    assert "业务库挂了" in result.message


@pytest.mark.asyncio
async def test_evaluate_batch_empty_returns_empty_summary() -> None:
    """空 batch 直接返回空 summary（不走 DB 查询）。"""
    session = _FakeSession({})
    dispatcher = DataQualityEvaluatorDispatcher()
    resp = await dispatcher.evaluateBatch(session, [])
    assert resp.summary_total == 0
    assert resp.summary_passed == 0
    assert resp.results == []


@pytest.mark.asyncio
async def test_evaluate_batch_mixed_pass_error(monkeypatch) -> None:
    """混合：已存在规则 PASS + 不存在 rule_id ERROR → summary_passed 只算 PASS。"""
    rule = _rule(rid=1, threshold="50.00")
    session = _FakeSession(rules={1: rule}, dss={100: _ds()})
    monkeypatch.setattr(
        "app.services.data_quality_evaluator.get_adapter",
        lambda did, ds: _StubAdapter(rows=[{"total": 10, "passed": 8}]),
    )
    dispatcher = DataQualityEvaluatorDispatcher()
    resp = await dispatcher.evaluateBatch(session, [1, 999])
    assert resp.summary_total == 2
    assert resp.summary_passed == 1
    statuses = {r.rule_id: r.status for r in resp.results}
    assert statuses[1] == "PASS"
    assert statuses[999] == "ERROR"


@pytest.mark.asyncio
async def test_evaluate_fail_status_populates_message_with_reason(monkeypatch) -> None:
    """feat-eval-fail-reason (2026-09-15)：rate < threshold → status=FAIL + 填 message。

    message 必须含：实际通过率、阈值、总数、通过数。
    PASS 时 message 仍为 None（保持旧契约）。
    """
    rule = _rule(rid=1, threshold="50.00")
    session = _FakeSession(rules={1: rule}, dss={100: _ds()})
    # total=10, passed=2 → rate=20% < threshold=50% → FAIL
    monkeypatch.setattr(
        "app.services.data_quality_evaluator.get_adapter",
        lambda did, ds: _StubAdapter(rows=[{"total": 10, "passed": 2}]),
    )
    dispatcher = DataQualityEvaluatorDispatcher()
    result = await dispatcher.evaluate(session, 1)

    assert result.status == "FAIL"
    assert result.message is not None
    # 关键子串：实际通过率 / 阈值 / 总数 / 通过数
    assert "20.00%" in result.message  # passRate
    assert "50.00" in result.message  # threshold
    assert "10" in result.message  # total
    assert "2" in result.message  # passed

    # 回归保护：rate >= threshold 时 message 仍为 None
    rule_pass = _rule(rid=2, threshold="50.00")
    session_pass = _FakeSession(
        rules={2: rule_pass}, dss={100: _ds()}
    )
    monkeypatch.setattr(
        "app.services.data_quality_evaluator.get_adapter",
        lambda did, ds: _StubAdapter(rows=[{"total": 10, "passed": 9}]),
    )
    result_pass = await dispatcher.evaluate(session_pass, 2)
    assert result_pass.status == "PASS"
    assert result_pass.message is None  # PASS 时不填 message