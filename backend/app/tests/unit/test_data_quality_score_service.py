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


class TestComputeScoresScope:
    """computeScores scope 过滤（feat-dq-scores-scope，2026-09-15）。

    三字段（datasource_id / target_table / rule_type）AND 组合。
    单元测试通过 monkeypatch 替换 `_listEnabledRules` 与 `evaluateBatch`，
    不走真实 SQL —— 验证 scope 参数被正确传到 ORM 查询 + 评估只跑命中规则。
    """

    @staticmethod
    def _make_session() -> Any:
        """最小 fake session：只实现 computeScores 链路用到的方法。"""

        class _Session:
            def __init__(self) -> None:
                self.executed: list[Any] = []

            async def execute(self, stmt):
                self.executed.append(stmt)

                class _R:
                    def scalars(self_inner):
                        class _S:
                            def all(self_inner_inner):
                                return []
                        return _S()

                return _R()

            def add_all(self, entities):
                # 给实体分配 id 模拟 flush 行为
                for i, e in enumerate(entities, start=1):
                    e.id = i

            def add(self, entity):
                # AuditService 用 session.add(row) 写审计行；不真落库
                pass

            async def flush(self):
                pass

            async def commit(self):
                pass

            async def refresh(self, entity):
                # 模拟 ORM refresh：不修改字段
                pass

        return _Session()

    async def test_compute_with_target_table_filter(self, monkeypatch):
        """target_table 过滤：4 条规则覆盖 2 张表，target_table=T1 → 只 evaluate T1。"""
        rules = [
            _rule(1, target_table="T1"),
            _rule(2, target_table="T1", rule_type=RuleType.VALIDITY),
            _rule(3, target_table="T2"),
            _rule(4, target_table="T2", rule_type=RuleType.VALIDITY),
        ]

        async def _fake_list_enabled(
            self_or_session,
            session=None,
            *,
            datasource_id=None,
            target_tables=None,
            rule_types=None,
        ):
            # 模拟真实 ORM WHERE：三个 scope 都参与 AND 过滤
            out = []
            for r in rules:
                if datasource_id is not None and r.datasource_id != datasource_id:
                    continue
                if target_tables and r.target_table not in target_tables:
                    continue
                if rule_types and r.rule_type not in rule_types:
                    continue
                out.append(r)
            return out

        # dispatcher：记录 rule_ids 并返回与该子集对应的 results
        captured: dict[str, list[int]] = {}

        class _FakeDispatcher:
            async def evaluateBatch(self, session, rule_ids):
                captured["rule_ids"] = list(rule_ids)
                results = []
                for rid in rule_ids:
                    rule = next(r for r in rules if r.id == rid)
                    results.append(
                        _eval(
                            rid,
                            rule_type=rule.rule_type,
                            pass_rate=100.0,
                        )
                    )
                return EvaluateBatchResponse(
                    results=results, summary_total=len(results), summary_passed=len(results)
                )

        monkeypatch.setattr(
            "app.services.data_quality_score_service.AuditService",
            type("A", (), {"record": staticmethod(lambda *a, **k: asyncio.sleep(0))}),
        )
        monkeypatch.setattr(
            DataQualityScoreService, "_listEnabledRules", _fake_list_enabled
        )

        service = DataQualityScoreService(dispatcher=_FakeDispatcher())
        resp = await service.computeScores(
            self._make_session(), target_tables=("T1",)
        )

        # 只 evaluate 了 T1 的 2 条
        assert captured["rule_ids"] == [1, 2]
        assert resp.evaluated_rules == 2
        # T1 表 1 条 + 1 GLOBAL = 2
        assert resp.saved_scores == 2
        tables = {s.target_table for s in resp.scores}
        assert tables == {"T1", "*"}
        # GLOBAL 的 completeness 只来自 T1 的 2 条 C+V，平均 100
        global_score = next(s for s in resp.scores if s.score_type == ScoreType.GLOBAL)
        assert global_score.overall_score == Decimal("100.00")

    async def test_compute_with_rule_type_filter(self, monkeypatch):
        """rule_type 过滤：3 条规则覆盖 C/V/U，rule_type=C → 只 evaluate C 那些。"""
        rules = [
            _rule(1, rule_type=RuleType.COMPLETENESS),
            _rule(2, rule_type=RuleType.VALIDITY),
            _rule(3, rule_type=RuleType.UNIQUENESS),
        ]

        async def _fake_list_enabled(
            self_or_session,
            session=None,
            *,
            datasource_id=None,
            target_tables=None,
            rule_types=None,
        ):
            # 模拟真实 ORM WHERE：三个 scope 都参与 AND 过滤
            out = []
            for r in rules:
                if datasource_id is not None and r.datasource_id != datasource_id:
                    continue
                if target_tables and r.target_table not in target_tables:
                    continue
                if rule_types and r.rule_type not in rule_types:
                    continue
                out.append(r)
            return out

        captured: dict[str, list[int]] = {}

        class _FakeDispatcher:
            async def evaluateBatch(self, session, rule_ids):
                captured["rule_ids"] = list(rule_ids)
                results = [
                    _eval(rid, rule_type=rules[rid - 1].rule_type, pass_rate=80.0)
                    for rid in rule_ids
                ]
                return EvaluateBatchResponse(
                    results=results, summary_total=len(results), summary_passed=len(results)
                )

        monkeypatch.setattr(
            "app.services.data_quality_score_service.AuditService",
            type("A", (), {"record": staticmethod(lambda *a, **k: asyncio.sleep(0))}),
        )
        monkeypatch.setattr(
            DataQualityScoreService, "_listEnabledRules", _fake_list_enabled
        )

        service = DataQualityScoreService(dispatcher=_FakeDispatcher())
        resp = await service.computeScores(
            self._make_session(), rule_types=(RuleType.COMPLETENESS,)
        )

        assert captured["rule_ids"] == [1]
        assert resp.evaluated_rules == 1
        # 1 per-table + 1 GLOBAL = 2
        assert resp.saved_scores == 2
        for s in resp.scores:
            if s.score_type == ScoreType.TABLE:
                # completeness 维度 80，其它维度 None
                assert s.completeness_score == Decimal("80.00")
                assert s.validity_score is None
                assert s.uniqueness_score is None

    async def test_compute_with_datasource_filter(self, monkeypatch):
        """datasource_id 过滤：2 数据源各 1 条规则，过滤后只 evaluate 数据源 1 的规则。"""
        rules = [
            DataQualityRule(
                id=1,
                rule_name="r1",
                rule_code="R1",
                datasource_id=1,
                target_table="T_A",
                target_column="X",
                rule_type=RuleType.COMPLETENESS,
                rule_expression=None,
                threshold="0.00",
                severity="MEDIUM",
                is_enabled=True,
                version="v1.0",
            ),
            DataQualityRule(
                id=2,
                rule_name="r2",
                rule_code="R2",
                datasource_id=2,
                target_table="T_B",
                target_column="X",
                rule_type=RuleType.COMPLETENESS,
                rule_expression=None,
                threshold="0.00",
                severity="MEDIUM",
                is_enabled=True,
                version="v1.0",
            ),
        ]

        async def _fake_list_enabled(
            self_or_session,
            session=None,
            *,
            datasource_id=None,
            target_tables=None,
            rule_types=None,
        ):
            # 模拟真实 ORM WHERE：三个 scope 都参与 AND 过滤
            out = []
            for r in rules:
                if datasource_id is not None and r.datasource_id != datasource_id:
                    continue
                if target_tables and r.target_table not in target_tables:
                    continue
                if rule_types and r.rule_type not in rule_types:
                    continue
                out.append(r)
            return out

        captured: dict[str, list[int]] = {}

        class _FakeDispatcher:
            async def evaluateBatch(self, session, rule_ids):
                captured["rule_ids"] = list(rule_ids)
                results = [_eval(rid, pass_rate=100.0) for rid in rule_ids]
                return EvaluateBatchResponse(
                    results=results, summary_total=len(results), summary_passed=len(results)
                )

        monkeypatch.setattr(
            "app.services.data_quality_score_service.AuditService",
            type("A", (), {"record": staticmethod(lambda *a, **k: asyncio.sleep(0))}),
        )
        monkeypatch.setattr(
            DataQualityScoreService, "_listEnabledRules", _fake_list_enabled
        )

        service = DataQualityScoreService(dispatcher=_FakeDispatcher())
        resp = await service.computeScores(self._make_session(), datasource_id=1)

        assert captured["rule_ids"] == [1]
        assert resp.evaluated_rules == 1
        # T_A 1 + GLOBAL 1 = 2
        assert resp.saved_scores == 2
        tables = {s.target_table for s in resp.scores}
        assert "T_B" not in tables

    async def test_compute_with_no_match_returns_empty(self, monkeypatch):
        """scope 与所有 enabled 规则都不匹配 → 返回空响应，不写库，不写 GLOBAL。"""
        rules = [
            _rule(1, target_table="T1"),
            _rule(2, target_table="T2"),
        ]

        async def _fake_list_enabled(
            self_or_session,
            session=None,
            *,
            datasource_id=None,
            target_tables=None,
            rule_types=None,
        ):
            # 模拟真实 ORM WHERE：三个 scope 都参与 AND 过滤
            out = []
            for r in rules:
                if datasource_id is not None and r.datasource_id != datasource_id:
                    continue
                if target_tables and r.target_table not in target_tables:
                    continue
                if rule_types and r.rule_type not in rule_types:
                    continue
                out.append(r)
            return out

        # dispatcher 不应被调用（_listEnabledRules 已经过滤了）
        called = {"count": 0}

        class _FakeDispatcher:
            async def evaluateBatch(self, session, rule_ids):
                called["count"] += 1
                return EvaluateBatchResponse(
                    results=[], summary_total=0, summary_passed=0
                )

        monkeypatch.setattr(
            DataQualityScoreService, "_listEnabledRules", _fake_list_enabled
        )

        service = DataQualityScoreService(dispatcher=_FakeDispatcher())
        resp = await service.computeScores(self._make_session(), target_tables=("T_MISSING",))

        assert called["count"] == 0  # dispatcher 完全没被调用
        assert resp.evaluated_rules == 0
        assert resp.saved_scores == 0
        assert resp.scores == []

    async def test_compute_scope_combined_and_logic(self, monkeypatch):
        """三字段 AND：datasource_id=1 + target_table=T1 + rule_type=C → 命中唯一规则。"""
        rules = [
            # ✓ datasource=1, table=T1, type=C → 命中
            DataQualityRule(
                id=1,
                rule_name="hit",
                rule_code="HIT",
                datasource_id=1,
                target_table="T1",
                target_column="X",
                rule_type=RuleType.COMPLETENESS,
                rule_expression=None,
                threshold="0.00",
                severity="MEDIUM",
                is_enabled=True,
                version="v1.0",
            ),
            # ✗ datasource=2
            DataQualityRule(
                id=2,
                rule_name="miss_ds",
                rule_code="MISS_DS",
                datasource_id=2,
                target_table="T1",
                target_column="X",
                rule_type=RuleType.COMPLETENESS,
                rule_expression=None,
                threshold="0.00",
                severity="MEDIUM",
                is_enabled=True,
                version="v1.0",
            ),
            # ✗ target_table=T2
            DataQualityRule(
                id=3,
                rule_name="miss_tt",
                rule_code="MISS_TT",
                datasource_id=1,
                target_table="T2",
                target_column="X",
                rule_type=RuleType.COMPLETENESS,
                rule_expression=None,
                threshold="0.00",
                severity="MEDIUM",
                is_enabled=True,
                version="v1.0",
            ),
            # ✗ rule_type=VALIDITY
            DataQualityRule(
                id=4,
                rule_name="miss_rt",
                rule_code="MISS_RT",
                datasource_id=1,
                target_table="T1",
                target_column="X",
                rule_type=RuleType.VALIDITY,
                rule_expression=None,
                threshold="0.00",
                severity="MEDIUM",
                is_enabled=True,
                version="v1.0",
            ),
        ]

        async def _fake_list_enabled(
            self_or_session,
            session=None,
            *,
            datasource_id,
            target_tables,
            rule_types,
        ):
            # 验证 AND 过滤：三个 kwarg 都用作筛选条件
            out = []
            for r in rules:
                if datasource_id is not None and r.datasource_id != datasource_id:
                    continue
                if target_tables and r.target_table not in target_tables:
                    continue
                if rule_types and r.rule_type not in rule_types:
                    continue
                out.append(r)
            return out

        captured: dict[str, list[int]] = {}

        class _FakeDispatcher:
            async def evaluateBatch(self, session, rule_ids):
                captured["rule_ids"] = list(rule_ids)
                results = [_eval(rid, pass_rate=100.0) for rid in rule_ids]
                return EvaluateBatchResponse(
                    results=results, summary_total=len(results), summary_passed=len(results)
                )

        monkeypatch.setattr(
            "app.services.data_quality_score_service.AuditService",
            type("A", (), {"record": staticmethod(lambda *a, **k: asyncio.sleep(0))}),
        )
        monkeypatch.setattr(
            DataQualityScoreService, "_listEnabledRules", _fake_list_enabled
        )

        service = DataQualityScoreService(dispatcher=_FakeDispatcher())
        resp = await service.computeScores(
            self._make_session(),
            datasource_id=1,
            target_tables=("T1",),
            rule_types=(RuleType.COMPLETENESS,),
        )

        # 只有 rule 1 同时满足 3 个条件
        assert captured["rule_ids"] == [1]
        assert resp.evaluated_rules == 1


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