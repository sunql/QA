"""Phase 1.4 getLatestTableScores 单元测试。

覆盖：
- 空 tuple → 空 dict（不查 DB）
- 异常 session → 静默降级返回空 dict（chat 主链路不挂）
- 非法 identifier → ValueError（拒绝脏数据，不静默吞）
- 正常路径：fake session 返回 N 行 → 正确组装 dict

集成测试在 test_chat_data_quality_integration.py 用真实 PG 5433 跑。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import ScoreType
from app.domain.exceptions import ValidationError
from app.domain.schemas import DataQualityBadge
from app.services.data_quality_score_service import DataQualityScoreService


def _run(coro):
    """Python 3.14 取消隐式 loop 创建，需手动驱动。"""
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeSession:
    """最小 fake session：execute(stmt, params) 返回预设 rows + 记录调用次数。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None, raise_exc: Exception | None = None):
        self.rows = rows or []
        self.raise_exc = raise_exc
        self.calls: list[Any] = []

    async def execute(self, stmt, params: Any = None):  # noqa: ARG002
        self.calls.append((stmt, params))
        if self.raise_exc is not None:
            raise self.raise_exc
        # 模拟 Result：.all() 返回 rows；支持 row._mapping 访问
        return SimpleNamespace(all=lambda: self.rows)


class TestEmptyInput:
    def test_empty_tuple_returns_empty_dict_no_db(self):
        """空 tuple：不查 DB、直接返回空 dict。"""
        svc = DataQualityScoreService()
        session = _FakeSession()
        result = _run(svc.getLatestTableScores(session, ()))
        assert result == {}
        assert session.calls == []  # 未触发 SQL

    def test_empty_tuple_with_white_space_only(self):
        """仅含空字符串的 tuple → 等价空集。"""
        svc = DataQualityScoreService()
        result = _run(svc.getLatestTableScores(_FakeSession(), ("",)))
        assert result == {}


class TestValidation:
    def test_rejects_invalid_identifier(self):
        """含非法字符（如分号、空格、连字符）→ 直接 raise ValidationError。

        关键：不像 DB 异常那样静默降级，因为这是 caller 的编程错误（selectedClasses
        应该是 ontology class 名，正常流程不会含分号）。一旦发生应该立刻暴露，
        不要被静默吞掉掩盖其他问题。
        """
        svc = DataQualityScoreService()
        with pytest.raises(ValidationError):
            _run(svc.getLatestTableScores(_FakeSession(), ("PORDER;DROP TABLE x",)))
        with pytest.raises(ValidationError):
            _run(svc.getLatestTableScores(_FakeSession(), ("has space",)))
        with pytest.raises(ValidationError):
            _run(svc.getLatestTableScores(_FakeSession(), ("a-b",)))


class TestExceptionSilentlyDegraded:
    def test_db_exception_returns_empty_dict_with_warn_log(self, caplog):
        """DB 异常 → 静默降级返回空 dict + 记 WARN 日志。

        关键：chat 主链路不能因 DQ 故障挂掉；后端通过日志告警供运维介入。
        """
        svc = DataQualityScoreService()
        session = _FakeSession(raise_exc=RuntimeError("connection refused"))
        with caplog.at_level("WARNING", logger="app.services.data_quality_score_service"):
            result = _run(svc.getLatestTableScores(session, ("PORDER",)))
        assert result == {}
        assert any("connection refused" in r.message or "DQ 评分查询失败" in r.message
                   for r in caplog.records)


class TestNormalPath:
    def test_single_table_evaluated(self):
        """单表已评估 → 返回 1 条 evaluated=True badge。"""
        svc = DataQualityScoreService()
        ts = datetime(2026, 8, 30, 2, 53, 54, tzinfo=timezone.utc)
        session = _FakeSession(
            rows=[
                {
                    "target_table": "PORDER",
                    "overall_score": Decimal("76.84"),
                    "evaluated_at": ts,
                    "rules_count": 5,
                }
            ]
        )
        result = _run(svc.getLatestTableScores(session, ("PORDER",)))
        assert "PORDER" in result
        badge = result["PORDER"]
        assert badge is not None
        assert badge.evaluated is True
        assert badge.target_table == "PORDER"
        assert badge.overall_score == Decimal("76.84")
        assert badge.evaluated_at == ts
        assert badge.rules_count == 5

    def test_table_not_evaluated_returns_false_badge(self):
        """查询的表从未评估 → 返回 evaluated=False badge（other fields None）。"""
        svc = DataQualityScoreService()
        # session 返回空行（该表无 score）
        session = _FakeSession(rows=[])
        result = _run(svc.getLatestTableScores(session, ("UNKNOWN_TABLE",)))
        assert "UNKNOWN_TABLE" in result
        badge = result["UNKNOWN_TABLE"]
        assert badge is not None
        assert badge.evaluated is False
        assert badge.overall_score is None
        assert badge.evaluated_at is None
        assert badge.rules_count is None

    def test_mixed_evaluated_and_not(self):
        """多表场景：部分评估 + 部分未评估，结果 dict 同时含 True/False。"""
        svc = DataQualityScoreService()
        ts = datetime(2026, 8, 30, tzinfo=timezone.utc)
        session = _FakeSession(
            rows=[
                {
                    "target_table": "PORDER",
                    "overall_score": Decimal("76.84"),
                    "evaluated_at": ts,
                    "rules_count": 5,
                }
            ]
        )
        result = _run(
            svc.getLatestTableScores(
                session, ("PORDER", "BPSUPPLIER", "MISSING_TABLE")
            )
        )
        assert set(result.keys()) == {"PORDER", "BPSUPPLIER", "MISSING_TABLE"}
        assert result["PORDER"].evaluated is True
        assert result["PORDER"].overall_score == Decimal("76.84")
        assert result["BPSUPPLIER"].evaluated is False
        assert result["MISSING_TABLE"].evaluated is False

    def test_multiple_scores_for_same_table_returns_latest(self):
        """同表多条 score → 仅返回 evaluated_at 最大的那条。"""
        svc = DataQualityScoreService()
        # fake session 假设 IN 子查询已过滤好；这里 fake 返回 1 行（最新）
        ts_new = datetime(2026, 8, 30, 2, 0, 0, tzinfo=timezone.utc)
        session = _FakeSession(
            rows=[
                {
                    "target_table": "PORDER",
                    "overall_score": Decimal("80.00"),
                    "evaluated_at": ts_new,
                    "rules_count": 5,
                }
            ]
        )
        result = _run(svc.getLatestTableScores(session, ("PORDER",)))
        assert result["PORDER"].overall_score == Decimal("80.00")

    def test_result_keys_preserve_order_match_input(self):
        """返回 dict 的 key 集合等于输入 tuple 去重后的集合。"""
        svc = DataQualityScoreService()
        ts = datetime(2026, 8, 30, tzinfo=timezone.utc)
        session = _FakeSession(
            rows=[
                {
                    "target_table": "T1",
                    "overall_score": Decimal("90.00"),
                    "evaluated_at": ts,
                    "rules_count": 3,
                },
                {
                    "target_table": "T2",
                    "overall_score": Decimal("50.00"),
                    "evaluated_at": ts,
                    "rules_count": 2,
                },
            ]
        )
        result = _run(svc.getLatestTableScores(session, ("T1", "T2", "T3")))
        assert set(result.keys()) == {"T1", "T2", "T3"}
        # 已评估的两条都有值
        assert result["T1"].evaluated is True
        assert result["T2"].evaluated is True
        assert result["T3"].evaluated is False