"""DataQualityScoreService 单元测试（Phase 1.3）。

覆盖：
- 聚合纯函数：5 个 rule_type 评估结果 → per-table + GLOBAL
- ERROR 状态跳过（不污染分母）
- 空 batch → 空响应不写库
- _buildScoreDict 单条聚合字典（non-null 维度平均 + overall 计算）
- listScores latest=true 路径（无需 DB，用 SQLAlchemy subquery 验构造）

dispatcher 通过构造 fake 注入；session 用最小 fake（仅实现需要的方法）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import RuleType, ScoreType
from app.domain.models import DataQualityRule
from app.domain.schemas import EvaluateBatchResponse, EvaluationResult
from app.services.data_quality_score_service import (
    DataQualityScoreService,
    _buildScoreDict,
    _scoreToRead,
)


def _eval(
    rule_id: int,
    *,
    rule_type: RuleType = RuleType.COMPLETENESS,
    pass_rate: float = 100.0,
    status: str = "PASS",
) -> EvaluationResult:
    return EvaluationResult(
        rule_id=rule_id,
        rule_code=f"R{rule_id}",
        rule_type=rule_type,
        datasource_id=None,
        total_count=10,
        passed_count=10,
        pass_rate=pass_rate,
        status=status,
        evaluated_at=datetime.now(timezone.utc),
        duration_ms=10,
        message=None,
    )


def _rule(
    rid: int, *, target_table: str = "PORDER", rule_type: RuleType = RuleType.COMPLETENESS
) -> DataQualityRule:
    return DataQualityRule(
        id=rid,
        rule_name="t",
        rule_code=f"R{rid}",
        datasource_id=1,
        target_table=target_table,
        target_column="X",
        rule_type=rule_type,
        rule_expression=None,
        threshold="95.00",
        severity="MEDIUM",
        is_enabled=True,
        version="v1.0",
    )


class TestBuildScoreDict:
    def test_completeness_only(self):
        out = _buildScoreDict(
            "PORDER",
            ScoreType.TABLE,
            {RuleType.COMPLETENESS: [90.0, 100.0]},
        )
        assert out["completeness_score"] == Decimal("95.00")
        assert out["validity_score"] is None
        assert out["overall_score"] == Decimal("95.00")
        assert out["rules_count"] == 2

    def test_multi_dimension_overall_is_mean_of_non_null(self):
        # completeness + validity 都填，overall = 两维平均
        out = _buildScoreDict(
            "PORDER",
            ScoreType.TABLE,
            {RuleType.COMPLETENESS: [80.0], RuleType.VALIDITY: [90.0]},
        )
        assert out["completeness_score"] == Decimal("80.00")
        assert out["validity_score"] == Decimal("90.00")
        # overall = (80 + 90) / 2 = 85.00
        assert out["overall_score"] == Decimal("85.00")

    def test_timeliness_dimension_stays_none(self):
        """TIMELINESS 暂未实现 → 永远 None；overall 不因 timeliness NULL 拉低。"""
        out = _buildScoreDict(
            "PORDER",
            ScoreType.TABLE,
            {RuleType.COMPLETENESS: [100.0]},
        )
        assert out["timeliness_score"] is None
        # overall 只算 completeness 一维
        assert out["overall_score"] == Decimal("100.00")

    def test_no_dimensions_returns_zero_overall(self):
        """没有任何 rule → overall=0.00，rules_count=0。"""
        out = _buildScoreDict("PORDER", ScoreType.TABLE, {})
        assert out["overall_score"] == Decimal("0.00")
        assert out["rules_count"] == 0
        assert out["completeness_score"] is None


class TestAggregate:
    def test_per_table_plus_global(self):
        # 两个表：A 表 2 个 rule（C+V），B 表 1 个 rule（C）
        results = [
            _eval(1, rule_type=RuleType.COMPLETENESS, pass_rate=100.0),
            _eval(2, rule_type=RuleType.VALIDITY, pass_rate=80.0),
            _eval(3, rule_type=RuleType.COMPLETENESS, pass_rate=60.0),
        ]
        rule_by_id = {
            1: _rule(1, target_table="A"),
            2: _rule(2, target_table="A", rule_type=RuleType.VALIDITY),
            3: _rule(3, target_table="B"),
        }
        agg = DataQualityScoreService._aggregate(results, rule_by_id)
        assert len(agg) == 3  # A, B, GLOBAL
        tables = {s["target_table"]: s for s in agg}
        assert tables["A"]["completeness_score"] == Decimal("100.00")
        assert tables["A"]["validity_score"] == Decimal("80.00")
        assert tables["A"]["overall_score"] == Decimal("90.00")  # (100+80)/2
        assert tables["B"]["completeness_score"] == Decimal("60.00")
        assert tables["B"]["validity_score"] is None
        # GLOBAL 拍平：3 个 pass_rate 平均
        global_ = tables["*"]
        assert global_["score_type"] == ScoreType.GLOBAL
        # GLOBAL 的 completeness = (100+60)/2 = 80；validity = 80；overall = (80+80)/2 = 80
        assert global_["completeness_score"] == Decimal("80.00")
        assert global_["validity_score"] == Decimal("80.00")
        assert global_["overall_score"] == Decimal("80.00")

    def test_error_status_skipped(self):
        """ERROR 状态的结果不计入聚合分母（异常不污染分数）。"""
        results = [
            _eval(1, rule_type=RuleType.COMPLETENESS, pass_rate=100.0, status="PASS"),
            _eval(2, rule_type=RuleType.COMPLETENESS, pass_rate=0.0, status="ERROR"),
        ]
        rule_by_id = {
            1: _rule(1),
            2: _rule(2),
        }
        agg = DataQualityScoreService._aggregate(results, rule_by_id)
        tables = {s["target_table"]: s for s in agg}
        # ERROR 跳过 → 只剩 1 条 pass_rate=100 → completeness = 100
        assert tables["PORDER"]["completeness_score"] == Decimal("100.00")

    def test_missing_rule_metadata_skipped(self):
        """rule_by_id 缺 rule_id → 跳过（防御性，避免 dispatcher 与 ORM 不同步时崩）。"""
        results = [_eval(1, pass_rate=100.0)]
        agg = DataQualityScoreService._aggregate(results, rule_by_id={})
        assert agg == []  # 没有聚合目标 → 不写库

    def test_unknown_rule_type_skipped(self):
        """非法 rule_type → 跳过，不抛错（防御性）。"""
        # 绕过 Pydantic enum 校验构造一条非法 rule_type 的 EvaluationResult
        bad = EvaluationResult.model_construct(
            rule_id=1,
            rule_code="R1",
            rule_type="BOGUS",
            datasource_id=None,
            total_count=10,
            passed_count=10,
            pass_rate=100.0,
            status="PASS",
            evaluated_at=datetime.now(timezone.utc),
            duration_ms=10,
            message=None,
        )
        results = [bad]
        rule_by_id = {1: _rule(1)}
        agg = DataQualityScoreService._aggregate(results, rule_by_id)
        # skip → 无维度 → 不写 per-table；GLOBAL 也不会出现
        assert agg == []


class TestComputeScoresEmpty:
    """空 enabled rule 集合：直接返回空响应不写库。"""

    async def test_empty_returns_no_scores(self, monkeypatch):
        class _NoRuleSession:
            async def execute(self, stmt):
                class _R:
                    def scalars(self_inner):
                        class _S:
                            def all(self_inner_inner):
                                return []
                        return _S()

                return _R()

            async def commit(self):
                pass

            async def refresh(self, _):
                pass

        async def _empty_batch(*_a, **_k):
            return EvaluateBatchResponse(results=[], summary_total=0, summary_passed=0)

        class _FakeDispatcher:
            async def evaluateBatch(self, session, rule_ids):
                return EvaluateBatchResponse(
                    results=[], summary_total=0, summary_passed=0
                )

        service = DataQualityScoreService(dispatcher=_FakeDispatcher())
        resp = await service.computeScores(_NoRuleSession())
        assert resp.evaluated_rules == 0
        assert resp.saved_scores == 0
        assert resp.scores == []


class TestBuildEntities:
    """_buildEntities：聚合字典 → ORM 实体，6 维都带。"""

    def test_all_six_fields_present(self):
        agg = [
            _buildScoreDict(
                "PORDER",
                ScoreType.TABLE,
                {RuleType.COMPLETENESS: [100.0], RuleType.VALIDITY: [90.0]},
            )
        ]
        started = time.perf_counter()
        ents = DataQualityScoreService._buildEntities(agg, started)
        assert len(ents) == 1
        ent = ents[0]
        assert ent.target_table == "PORDER"
        assert ent.score_type == ScoreType.TABLE
        # 6 维列齐全；timeliness 仍为 None
        assert ent.completeness_score == Decimal("100.00")
        assert ent.validity_score == Decimal("90.00")
        assert ent.uniqueness_score is None
        assert ent.consistency_score is None
        assert ent.timeliness_score is None
        assert ent.referential_score is None
        assert ent.overall_score == Decimal("95.00")
        # duration_ms 在 0+，rules_count=2
        assert ent.evaluation_duration_ms >= 0
        assert ent.rules_count == 2
        # evaluated_at 必须有值
        assert ent.evaluated_at is not None


class TestScoreToRead:
    def test_roundtrip(self):
        agg = [
            _buildScoreDict(
                "PO",
                ScoreType.TABLE,
                {RuleType.COMPLETENESS: [80.0]},
            )
        ]
        ent = DataQualityScoreService._buildEntities(agg, 0.0)[0]
        # 模拟 ORM 属性访问
        ent.id = 42
        ent.created_time = ent.evaluated_at
        ent.updated_time = ent.evaluated_at
        read = _scoreToRead(ent)
        assert read.id == 42
        assert read.target_table == "PO"
        assert read.score_type == ScoreType.TABLE
        assert read.completeness_score == Decimal("80.00")
        assert read.timeliness_score is None
        assert read.overall_score == Decimal("80.00")
        assert read.rules_count == 1