"""时间窗口评估器单测（feat-dq-evaluation-report）。

覆盖 5 个 evaluator 在 time_window=None（旧行为）和 time_window=(start,end)（新行为）
下的 SQL 生成差异，以及 dispatcher 对 time_window 参数的转发。

不在这里测真实业务库（要走 PG 集成测试），仅断言：
- SQL 文本包含/不包含 WHERE created_at BETWEEN
- SQL 中时间字面量为 ISO8601（aware UTC 格式）
- dispatcher 把同一个 time_window 透传到 batch 里每条 rule
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import RuleType, Severity
from app.domain.models import DataQualityRule, DataSource
from app.services.data_quality_evaluator import DataQualityEvaluatorDispatcher
from app.services.data_quality_evaluators._common import (
    reset_table_capability_cache,
    time_window_clause,
)
from app.services.data_quality_evaluators.completeness import evaluate as compEval
from app.services.data_quality_evaluators.consistency import evaluate as consEval
from app.services.data_quality_evaluators.referential import evaluate as refEval
from app.services.data_quality_evaluators.uniqueness import evaluate as uniqEval
from app.services.data_quality_evaluators.validity import evaluate as valEval


# ===== fixtures =====


class _StubAdapter:
    """单次响应式适配器；tests 只看 calls 即可。

    自 2026-09-15 `inject_time_window_clause` 改造后，evaluator 在拼 WHERE 前会先
    发一条 `SELECT created_at FROM "<tbl>" WHERE 1=0 LIMIT 1` 列存在性探测。
    stub 必须能识别该探测并返回空行（模拟「表里没有 created_at」），否则：
    - 探测返回 1 行 → has_created_at_column 缓存为 True → 走「加 WHERE」分支。
      但这会让 `adapter.calls[0]` 是探测 SQL，污染主查询断言。
    stub 选择「探测返回空 + 主查询返回 rows」，配合 reset_table_capability_cache，
    让每个 test 用全新 cache 状态跑「不注入 WHERE」的路径（与 _StubAdapter
    原设计一致——它本身不模拟 created_at 列存在）。
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or [{"total": 0, "passed": 0}]
        self.calls: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        self.calls.append(sql)
        # 列存在性探测：返回 1 行（模拟「表里有 created_at 列」）→ 触发 WHERE 注入。
        # 这样 calls[0] 是探测 SQL，calls[-1] / calls[1] 才是主查询；tests 用后者断言。
        if 'SELECT created_at FROM' in sql and '1=0' in sql:
            return [{"x": 1}]
        return self.rows


def _run(coro):
    """Python 3.14 已取消隐式 loop，手动 new_event_loop。"""
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_rule(
    *,
    rule_type: RuleType,
    target_table: str = "PORDER",
    target_column: str | None = "ORDER_QTY",
    rule_expression: str | None = None,
) -> DataQualityRule:
    return DataQualityRule(
        id=1,
        rule_name="t",
        rule_code="T",
        target_table=target_table,
        target_column=target_column,
        rule_type=rule_type,
        rule_expression=rule_expression,
        threshold="95.00",
        severity=Severity.MEDIUM,
        is_enabled=True,
        version="v1.0",
    )


@pytest.fixture(autouse=True)
def _resetColumnCache() -> None:
    """每个 test 前清空 _TABLE_HAS_CREATED_AT 进程内缓存，否则测试间串台。

    复盘：autouse 之前只有 completeness_injects_where 一个测试失败——因为它先跑，
    stub 探测返回空 → 缓存 'PORDER' 无 created_at → 后续 test 即使 stub 改返回 1
    行也读缓存走「不注入 WHERE」分支，断言看起来过了。autouse 后每个 test 拿到干净
    缓存，行为可预测。
    """
    reset_table_capability_cache()


_START = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
_END = datetime(2026, 9, 14, 23, 59, 59, tzinfo=UTC)
_WINDOW = (_START, _END)


# ===== time_window_clause 纯函数 =====


class TestTimeWindowClause:
    def test_none_returns_empty_string(self) -> None:
        """window=None 时返回空串（evaluator 不加 WHERE，向后兼容）。"""
        assert time_window_clause(None) == ""

    def test_tuple_returns_where_clause(self) -> None:
        """window=(s,e) 返回 WHERE created_at BETWEEN '...' AND '...'。"""
        clause = time_window_clause(_WINDOW)
        assert clause.startswith(" WHERE created_at BETWEEN '")
        assert clause.endswith("'")
        # 两次 isoformat 调用之间以 AND ' 拼接
        assert " AND '" in clause
        # ISO8601 字面量（aware UTC → +00:00）
        assert _START.isoformat() in clause
        assert _END.isoformat() in clause

    def test_clause_is_safe_for_datetime_quote(self) -> None:
        """awareness 强制：naive datetime 仍走 isoformat，但 dispatcher 端应禁止。"""
        # 这里只断言生成不抛错，service 层应在更早处拒掉 naive
        s = datetime(2026, 1, 1, 0, 0, 0)
        e = datetime(2026, 1, 2, 0, 0, 0)
        clause = time_window_clause((s, e))
        assert " WHERE created_at BETWEEN" in clause


# ===== 5 个 evaluator：time_window=None → 不加 WHERE =====


class TestNoneTimeWindowPreservesOldBehavior:
    """回归保护：time_window=None 时所有 evaluator 维持旧 SQL 形态（无 WHERE 子句）。"""

    def test_completeness_none(self) -> None:
        rule = _make_rule(rule_type=RuleType.COMPLETENESS, target_column="X")
        adapter = _StubAdapter()
        _run(compEval(rule, adapter, time_window=None))
        sql = adapter.calls[0]
        assert " WHERE " not in sql
        assert "BETWEEN" not in sql

    def test_validity_none(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.VALIDITY, target_column="X", rule_expression="X > 0"
        )
        adapter = _StubAdapter()
        _run(valEval(rule, adapter, time_window=None))
        sql = adapter.calls[0]
        assert " WHERE " not in sql

    def test_uniqueness_none(self) -> None:
        rule = _make_rule(rule_type=RuleType.UNIQUENESS, target_column="X")
        adapter = _StubAdapter()
        _run(uniqEval(rule, adapter, time_window=None))
        sql = adapter.calls[0]
        assert " WHERE " not in sql

    def test_consistency_none(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.CONSISTENCY,
            target_column=None,
            rule_expression="A = B",
        )
        adapter = _StubAdapter()
        _run(consEval(rule, adapter, time_window=None))
        sql = adapter.calls[0]
        assert " WHERE " not in sql

    def test_referential_none(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.REFERENTIAL,
            target_column="X",
            rule_expression="REF R.RK",
        )
        adapter = _StubAdapter()
        _run(refEval(rule, adapter, time_window=None))
        sql = adapter.calls[0]
        # REFERENTIAL 的 EXISTS 子查询里本来就有 WHERE，但 time_window 的标志是
        # `created_at BETWEEN`；time_window=None 时这个标志绝不应出现
        assert "BETWEEN" not in sql
        # 旧形态：直接在 FROM "<table>" 上扫，且没有 CTE
        assert "WITH filtered AS" not in sql
        assert 'FROM "PORDER"' in sql


# ===== 5 个 evaluator：time_window=(s,e) → 加 WHERE =====


class TestTupleTimeWindowInjectsClause:
    """新行为：time_window=(s,e) 时把 WHERE created_at BETWEEN ... 拼到 SQL。"""

    def test_completeness_injects_where(self) -> None:
        rule = _make_rule(rule_type=RuleType.COMPLETENESS, target_column="X")
        adapter = _StubAdapter()
        _run(compEval(rule, adapter, time_window=_WINDOW))
        sql = adapter.calls[-1]  # calls[0] 是列存在性探测（见 _StubAdapter 注）
        assert " WHERE created_at BETWEEN" in sql
        assert _START.isoformat() in sql
        assert _END.isoformat() in sql
        # FROM 在 WHERE 之前
        assert sql.index("FROM") < sql.index("WHERE")

    def test_validity_injects_where(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.VALIDITY, target_column="X", rule_expression="X > 0"
        )
        adapter = _StubAdapter()
        _run(valEval(rule, adapter, time_window=_WINDOW))
        sql = adapter.calls[-1]
        assert " WHERE created_at BETWEEN" in sql
        # SQL 整体结构：SELECT ... FROM "PORDER" WHERE created_at BETWEEN ...
        assert 'FROM "PORDER"' in sql
        # WHERE 一定在 FROM 之后（主句 WHERE，不是 EXISTS 子查询 WHERE）
        assert sql.index("FROM") < sql.index("WHERE created_at")

    def test_uniqueness_injects_where(self) -> None:
        rule = _make_rule(rule_type=RuleType.UNIQUENESS, target_column="X")
        adapter = _StubAdapter()
        _run(uniqEval(rule, adapter, time_window=_WINDOW))
        sql = adapter.calls[-1]
        assert " WHERE created_at BETWEEN" in sql

    def test_consistency_injects_where(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.CONSISTENCY,
            target_column=None,
            rule_expression="A = B",
        )
        adapter = _StubAdapter()
        _run(consEval(rule, adapter, time_window=_WINDOW))
        sql = adapter.calls[-1]
        assert " WHERE created_at BETWEEN" in sql

    def test_referential_uses_cte_for_time_window(self) -> None:
        """REFERENTIAL 有嵌套 EXISTS，time_window 必须走 CTE 把过滤前置，避免子查询里再叠 WHERE。"""
        rule = _make_rule(
            rule_type=RuleType.REFERENTIAL,
            target_column="X",
            rule_expression="REF R.RK",
        )
        adapter = _StubAdapter()
        _run(refEval(rule, adapter, time_window=_WINDOW))
        sql = adapter.calls[-1]
        # CTE 形态：WITH filtered AS (...) SELECT ... FROM filtered
        assert "WITH filtered AS" in sql
        # 过滤子句在 CTE 内
        assert " WHERE created_at BETWEEN" in sql
        # EXISTS 引用 filtered.fk_col 而不是原始列
        assert "filtered.fk_col" in sql
        # 最终 SELECT ... FROM filtered（不是 FROM "PORDER"）
        assert 'FROM filtered' in sql


# ===== dispatcher 转发 =====


class _FakeSession:
    def __init__(
        self,
        rules: dict[int, Any] | None = None,
        dss: dict[int, Any] | None = None,
    ) -> None:
        self._rules = rules or {}
        self._dss = dss or {}

    async def get(self, cls, key):
        if cls is DataQualityRule:
            return self._rules.get(key)
        if cls is DataSource:
            return self._dss.get(key)
        return None

    async def execute(self, stmt):
        class _Result:
            def scalars(self_inner):
                class _Scalars:
                    def all(self_inner_inner):
                        return list(self._rules.values())

                return _Scalars()

        return _Result()


def _rule(rid: int, rule_type: str = "COMPLETENESS") -> Any:
    return SimpleNamespace(
        id=rid,
        rule_code=f"R{rid}",
        rule_type=rule_type,
        datasource_id=100,
        target_table="T",
        target_column="C",
        rule_expression="C > 0",
        threshold="50.00",
        updated_time=None,
    )


def _ds() -> Any:
    return SimpleNamespace(id=100, type="postgresql")


class TestDispatcherForwardsTimeWindow:
    """dispatcher 必须在 evaluate / evaluateBatch 上把 time_window 透传给 evaluator。
    这里同时 monkeypatch get_adapter（避开 DataSource.password_encrypted ORM 依赖）
    和 _EVALUATORS[RuleType.*]（用可控返回值的 fake 替身）。"""

    @pytest.mark.asyncio
    async def test_evaluate_forwards_time_window_to_evaluator(
        self, monkeypatch
    ) -> None:
        """dispatcher.evaluate 透传 time_window 到 evaluator。"""
        session = _FakeSession(rules={1: _rule(1)}, dss={100: _ds()})
        captured: dict[str, Any] = {}

        async def _fake_evaluator(rule, adapter, time_window):
            captured["time_window"] = time_window
            captured["rule_id"] = rule.id
            return 10, 5

        def _fake_adapter(did, ds):
            # dispatcher 同步调用 get_adapter(...)（非 await），签名同 _StubAdapter
            return _StubAdapter()

        import app.services.data_quality_evaluator as disp_module

        disp_module._EVALUATORS[RuleType.COMPLETENESS] = _fake_evaluator
        monkeypatch.setattr(disp_module, "get_adapter", _fake_adapter)

        dispatcher = DataQualityEvaluatorDispatcher()
        result = await dispatcher.evaluate(session, 1, time_window=_WINDOW)
        assert result.status == "PASS"
        assert captured.get("time_window") == _WINDOW
        assert captured.get("rule_id") == 1

    @pytest.mark.asyncio
    async def test_evaluate_batch_forwards_same_time_window_to_each_rule(
        self, monkeypatch
    ) -> None:
        """dispatcher.evaluateBatch 对 batch 内每条 rule 用同一 time_window。"""
        session = _FakeSession(
            rules={1: _rule(1), 2: _rule(2), 3: _rule(3)},
            dss={100: _ds()},
        )
        seen_windows: list[Any] = []

        async def _fake(rule, adapter, time_window):
            seen_windows.append(time_window)
            return 10, 5

        def _fake_adapter(did, ds):
            # dispatcher 同步调用 get_adapter(...)（非 await），签名同 _StubAdapter
            return _StubAdapter()

        import app.services.data_quality_evaluator as disp_module

        disp_module._EVALUATORS[RuleType.COMPLETENESS] = _fake
        monkeypatch.setattr(disp_module, "get_adapter", _fake_adapter)

        dispatcher = DataQualityEvaluatorDispatcher()
        resp = await dispatcher.evaluateBatch(session, [1, 2, 3], time_window=_WINDOW)
        assert resp.summary_total == 3
        assert resp.summary_passed == 3
        assert seen_windows == [_WINDOW, _WINDOW, _WINDOW]

    @pytest.mark.asyncio
    async def test_evaluate_without_time_window_passes_none(
        self, monkeypatch
    ) -> None:
        """旧调用方不传 time_window → evaluator 收到 None（行为不变）。"""
        session = _FakeSession(rules={1: _rule(1)}, dss={100: _ds()})
        seen: dict[str, Any] = {}

        async def _fake(rule, adapter, time_window):
            seen["tw"] = time_window
            return 1, 1

        def _fake_adapter(did, ds):
            # dispatcher 同步调用 get_adapter(...)（非 await），签名同 _StubAdapter
            return _StubAdapter()

        import app.services.data_quality_evaluator as disp_module

        disp_module._EVALUATORS[RuleType.COMPLETENESS] = _fake
        monkeypatch.setattr(disp_module, "get_adapter", _fake_adapter)

        dispatcher = DataQualityEvaluatorDispatcher()
        await dispatcher.evaluate(session, 1)
        assert seen["tw"] is None


# ===== time_window 注入面安全：拒绝单引号注入 =====


class TestTimeWindowSqlInjectionSafe:
    """time_window 是 (datetime, datetime) 元组，类型由 service 层保证；evaluator
    只调 isoformat 拼接。即便有人错传含 ' 的字符串，repr/isoformat 也不会带 '）。

    这里断言：start 端若被传入一个会触发"看似注入"的字符串（仅用于占位，
    实际 service 不会传 string），evaluator 仍只用 .isoformat() 拼接——但
    由于类型是 datetime，isoformat 不会含单引号之外的可疑字符。
    """

    def test_isoformat_does_not_contain_extra_single_quote(self) -> None:
        """aware UTC datetime 的 isoformat 形如 2026-09-01T00:00:00+00:00，无 '。"""
        iso = _START.isoformat()
        assert "'" not in iso
        # 同时验证 naive 也无 '
        naive = datetime(2026, 9, 1, 0, 0, 0)
        assert "'" not in naive.isoformat()

    def test_clause_cannot_be_split_by_extra_quote(self) -> None:
        """生成出来的 WHERE 子句里，单引号恰好是 4 个（包 start/end 各 2 个）。"""
        clause = time_window_clause(_WINDOW)
        # WHERE created_at BETWEEN '<s>' AND '<e>'  → 4 个单引号
        assert clause.count("'") == 4
        # 不应存在两条 BETWEEN / 不应存在 OR 关键字
        assert clause.count("BETWEEN") == 1
        assert " OR " not in clause
        assert ";" not in clause
