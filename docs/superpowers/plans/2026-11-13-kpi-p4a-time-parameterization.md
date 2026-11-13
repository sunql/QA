# KPI P4a 时间参数化（`{time_scope}` 占位符）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 显式时间限定问句在指标 formula 声明 `{time_scope}` 占位符 + time_column 存在时走 Tier A，服务端确定性渲染时间谓词；未改写存量公式行为零变化。

**Architecture:** 纯函数解析（`time_scope_resolver` 扩展）→ 纯函数渲染（新 `time_scope_renderer`，Oracle 先行、其余 fail-closed）→ 路由决策表加 `time_param_ready` 参数 → `chat_service._executeTierA` 渲染接入 → 治理写入校验 + dry-run 改写脚本。全程无 LLM 参与时间计算。

**Tech Stack:** Python 3.12 / SQLAlchemy async / 纯标准库日期计算 / pytest

**Spec:** `docs/superpowers/specs/2026-11-13-kpi-time-parameterization-design.md`

## Global Constraints

- **不部署、不改生产库**；本批无 Alembic 迁移、无 DDL。
- 时间形态（D3 裁决）：近 N 月/季/年 + 本月/上月/今年/去年；**具体日期区间、week 粒度不做**。
- 方言（D4 裁决）：Oracle 先行；PG/MySQL 渲染抛 `TimeScopeRenderUnsupportedError` ⇒ degraded，**绝不静默降级 Tier B 让 LLM 猜时间**。
- 渲染口径：用 resolver 算好的半开区间 `[start, end)` 渲染 **ANSI DATE 字面量**：`{col} >= DATE '2026-06-01' AND {col} < DATE '2026-12-01'`（spec §5.2「等价确定性表达式」裁决；确定性、可断言整段子句）。
- 「近 N 月」按**自然月对齐**（含本月共 N 个自然月，`start = 本月初 -(N-1)`），与 nl2sql 侧 Oracle 模板先例一致（见 `Harness/wiki` 时间窗口章节）。
- 谓词断言必须**打整段子句**（防 ASC/DESC 式漂移假绿）。
- 占位符缺失 ⇒ 行为零变化（路由仍 `REASON_EXPLICIT_TIME_SCOPE` → Tier B）。
- 渲染失败（解析失败/方言不支持）⇒ 返回 `(None, reason)` 走 Tier B 哨兵，绝不把含 `{time_scope}` 原文的 formula 发给数据库。
- 测试环境：真实 PG `TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'`、`TEST_NEO4J_URI='bolt://127.0.0.1:7688'`、`TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'`；**不跑 Milvus/Neo4j 危险套件**；定向 pytest（不跑全量门禁）；停 pytest 用 SIGINT；绝不并发两个 pytest。
- 派发 subagent 必须显式写 model；worktree `/private/tmp/qa-kpi-p4a`（分支 `feat/kpi-p4a-time-parameterization`）。
- 命名：函数/变量 camelCase、常量 UPPER_SNAKE_CASE；不可变（frozen dataclass / 新对象）。

## Recon 事实（已实证，写代码前照此对照）

| 事实 | 位置 |
|---|---|
| `TimeScope(matched, start, end, label, granularity: str)` frozen dataclass；`parseTimeScope/parseTimeScopes(question, today=None)` | `backend/app/services/time_scope_resolver.py:96-255` |
| 模块 docstring :8-13 写着「滚动窗口不解析」——本批**推翻**该约束（D3 裁决），须更新 docstring | `time_scope_resolver.py:8-13` |
| `planKpiExecution(...)` 纯函数决策表；:84 `if has_explicit_time_scope: return KpiExecutionPlan(TIER_B, REASON_EXPLICIT_TIME_SCOPE)` | `backend/app/services/kpi_execution_service.py:55-90` |
| `hasExplicitTimeScope(question)` 检测正则 `_EXPLICIT_TIME_PATTERNS`（本批**不改**检测口径） | 同文件 :100-137 |
| `_planKpiTier` 有 3 个 `planKpiExecution` 调用点（开关关 :413 / kpi None :419 / 正常 :440）；question 变量 = `dto.question` | `backend/app/services/chat_stream_output.py:404-459` |
| 真实 `_executeTierA(match, kpi, session)` 在 chat_service；查 metric/datasource 后 `execute(formula=metric.formula or "")`；调用点 :595 `await self._executeTierA(match, kpi, session)` | `backend/app/services/chat_service.py:1589-1638`、`chat_stream_output.py:595` |
| 占位实现 `_executeTierA` 返回 `(None, REASON_EXECUTION_ERROR)` | `chat_stream_output.py:461-469` |
| **测试桩 3 参签名** `async def _executeTierA(self, match, kpi, session)`（:65/:176）+ 直调 :194 —— 签名加参必须同步改桩，否则 `TypeError` | `backend/app/tests/unit/test_chat_tier_a_preamble.py` |
| `DataSourceType` 枚举值小写：`mysql`/`postgresql`/`oracle`；`DataSource.type` 列 | `backend/app/domain/enums.py:29-34` |
| `createMetric` :908 / `updateMetric` :1022（partial update，`updates.get("formula", entity.formula)` 合并模式）；`ValidationError` ⇒ HTTP 422（`statusForError` 唯一映射源） | `backend/app/services/ontology_service.py`、`backend/app/domain/exceptions.py:206-220` |
| 治理写 gate 集成测试先例：`ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}` | `backend/app/tests/integration/test_ontology_metric_write_gate.py:17-19` |
| 回填脚本两层先例：纯函数 `app/services/kpi_time_grain_backfill.py` + CLI `scripts/kpi_backfill_time_grain.py` | 本批改写脚本照此分两层 |
| messages 惯例：`MSG_XXX = "中文文案"` 于 `backend/app/services/messages_zh.py`（新消息**避免字面花括号**，防 `.format` 转义雷） | 全文件 |

---

### Task 1: resolver 扩展 —— 近 N 月/季/年 + 自然月/年

**Files:**
- Modify: `backend/app/services/time_scope_resolver.py`
- Test: `backend/app/tests/unit/test_time_scope_resolver.py`（追加）

**Interfaces:**
- Consumes: 现有 `TimeScope`、`_selectNonOverlapping`、`_collectCandidates`、`_RELATIVE_YEAR_OFFSETS`。
- Produces: `parseTimeScope`/`parseTimeScopes` 可解析新形态；`_cnNumberToInt(token: str) -> int | None`；`_addMonths(d: date, n: int) -> date`（供本文件内部与 Task 2 无关）。Task 3/4 只消费既有 `parseTimeScope`。

- [ ] **Step 1: RED —— 追加测试**（追加到 `test_time_scope_resolver.py` 末尾）：

```python
# =============================================================================
# P4a：滚动窗口 + 自然月/年（设计 D3 裁决；此前滚动窗口不解析，2026-11-13 改判）
# =============================================================================


class TestRecentWindowScopes:
    def test_recent_n_months_is_natural_month_aligned(self) -> None:
        """近6个月 = 含本月共 6 个自然月，半开区间 [本月初-5月, 下月初)。"""
        scope = parseTimeScope("近6个月供应商准时交付率", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.start == date(2026, 6, 1)
        assert scope.end == date(2026, 12, 1)
        assert scope.granularity == "month"
        assert "近6个月" in scope.matched

    def test_recent_one_month_starts_at_current_month(self) -> None:
        scope = parseTimeScope("近1个月采购额", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.start == date(2026, 11, 1)
        assert scope.end == date(2026, 12, 1)

    def test_recent_n_months_crosses_year(self) -> None:
        scope = parseTimeScope("近6个月交付率", today=date(2026, 1, 5))
        assert scope is not None
        assert scope.start == date(2025, 8, 1)
        assert scope.end == date(2026, 2, 1)

    def test_recent_cn_numeral_months(self) -> None:
        scope = parseTimeScope("最近三个月销售额", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.start == date(2026, 9, 1)
        assert scope.end == date(2026, 12, 1)

    def test_recent_liang_means_two(self) -> None:
        scope = parseTimeScope("近两个月退货率", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.start == date(2026, 10, 1)

    def test_recent_n_quarters(self) -> None:
        scope = parseTimeScope("近2个季度库存周转", today=date(2026, 11, 13))
        assert scope is not None
        # 当前季度 Q4 起点 10-01，-(2-1) 季 ⇒ 7-01
        assert scope.start == date(2026, 7, 1)
        assert scope.end == date(2027, 1, 1)
        assert scope.granularity == "quarter"

    def test_recent_n_years(self) -> None:
        scope = parseTimeScope("近3年营收", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.start == date(2024, 1, 1)
        assert scope.end == date(2027, 1, 1)
        assert scope.granularity == "year"

    def test_recent_zero_or_invalid_not_parsed(self) -> None:
        assert parseTimeScope("近0个月销售额", today=date(2026, 11, 13)) is None


class TestStandaloneNaturalScopes:
    def test_ben_yue(self) -> None:
        scope = parseTimeScope("本月供应商数量", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.start == date(2026, 11, 1)
        assert scope.end == date(2026, 12, 1)

    def test_shang_ge_yue(self) -> None:
        scope = parseTimeScope("上个月采购额", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.start == date(2026, 10, 1)
        assert scope.end == date(2026, 11, 1)

    def test_jin_nian_bare(self) -> None:
        scope = parseTimeScope("今年入库总量", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.start == date(2026, 1, 1)
        assert scope.end == date(2027, 1, 1)
        assert scope.granularity == "year"

    def test_qu_nian_bare(self) -> None:
        scope = parseTimeScope("去年出货总额", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.start == date(2025, 1, 1)
        assert scope.end == date(2026, 1, 1)


class TestResolverInteractionGuards:
    def test_year_quarter_beats_bare_year(self) -> None:
        """「今年一季度」须命中季度口径（长匹配优先），不落裸年全年口径。"""
        scope = parseTimeScope("今年一季度采购额", today=date(2026, 11, 13))
        assert scope is not None
        assert scope.granularity == "quarter"
        assert scope.start == date(2026, 1, 1)
        assert scope.end == date(2026, 4, 1)

    def test_concrete_date_range_still_not_parsed(self) -> None:
        """D3 边界：具体日期区间仍不解析（后续批次）。"""
        assert parseTimeScope("2026-01-01 到 2026-03-31 的销售额", today=date(2026, 11, 13)) is None


class TestCnNumberToInt:
    def test_simple_and_combinations(self) -> None:
        assert _cnNumberToInt("3") == 3
        assert _cnNumberToInt("三") == 3
        assert _cnNumberToInt("两") == 2
        assert _cnNumberToInt("十") == 10
        assert _cnNumberToInt("十二") == 12
        assert _cnNumberToInt("二十") == 20
        assert _cnNumberToInt("二十一") == 21

    def test_invalid_returns_none(self) -> None:
        assert _cnNumberToInt("百") is None
        assert _cnNumberToInt("") is None
```

（顶部 import 补 `_cnNumberToInt`——从 `app.services.time_scope_resolver import` 清单里加。）

- [ ] **Step 2: 跑 RED**

Run: `cd /private/tmp/qa-kpi-p4a/backend && export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://127.0.0.1:7688' TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026' && pytest app/tests/unit/test_time_scope_resolver.py -q`
Expected: 新增用例全 FAIL（`ImportError: _cnNumberToInt` / `assert scope is not None`），存量用例全 PASS。

- [ ] **Step 3: GREEN —— resolver 实现**（`time_scope_resolver.py`）：

(a) 更新模块 docstring :8-13 的旧约束段为：

```python
设计约束：
- 无歧义口径确定性解析；「这半年」等语义仍不解析（返回 None）。
- P4a（2026-11-13，设计 D3 裁决）：滚动窗口「近 N 月/季/年」按**自然月/季/年
  对齐**解析（含当前期共 N 期，[start, end) 半开）；「本月/上月/今年/去年」
  解析为自然区间。具体日期区间（2026-01-01 到 …）仍不解析。
- 半开区间含起不含止，跨年边界由计算得出，不允许调用方自行拼 `end`。
- 命中多个区间时按「长匹配优先、再按出现位置」去重。
```

(b) 中文数字（放在 `_CN_QUARTERS` 之后）：

```python
# 滚动窗口 N 的中文数字（业务窗口 N 不会过百，「十/十二/二十一」已覆盖）。
_CN_WINDOW_DIGITS: dict[str, int] = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cnNumberToInt(token: str) -> int | None:
    """中文/阿拉伯数字 → 正整数；不支持百以上，非法返回 None。"""
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        left, _, right = token.partition("十")
        if (left and left not in _CN_WINDOW_DIGITS) or (right and right not in _CN_WINDOW_DIGITS):
            return None
        tens = _CN_WINDOW_DIGITS.get(left, 1)
        ones = _CN_WINDOW_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_WINDOW_DIGITS.get(token)


def _addMonths(d: date, n: int) -> date:
    """月份加减（纯标准库）；本模块只传月初 1 号，无月末 clamp 路径。"""
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, d.day)
```

(c) 新正则（放在 `_RE_HALF_YEAR` 之后）：

```python
# 近 N 月/季/年（P4a D3）：近6个月 / 最近三个月 / 过去 12 个月 / 前3个季度 / 近2年
_RE_RECENT_N = re.compile(
    r"(?P<dir>近|最近|过去|前)\s*(?P<n>[0-9]+|[一二三四五六七八九十两]+)\s*个?\s*(?P<unit>月|季度|年)"
)
# 独立自然月：本月 / 这个月 / 上个月 / 当前月（「上季度」由 _RE_RELATIVE_QUARTER 接管，不冲突）
_RE_STANDALONE_MONTH = re.compile(r"(?P<dir>当前|上一|这个|上个|上|本|这)(?:个)?月")
# 裸相对年（不接季度后缀的场景）：今年销售额 / 去年出货。「今年一季度」
# 由季度规则更长匹配胜出（_selectNonOverlapping）。
_RE_BARE_YEAR = re.compile(rf"(?P<rel>{_RELATIVE_YEAR})")
```

(d) 窗口构造（放在 `_halfYearScope` 之后）：

```python
def _recentWindowScope(today: date, n: int, unit: str, matched: str) -> TimeScope | None:
    """滚动近 N 月/季/年：含当前期共 N 期，自然边界对齐，[start, end)。"""
    if n <= 0:
        return None
    if unit == "月":
        monthStart = date(today.year, today.month, 1)
        start = _addMonths(monthStart, -(n - 1))
        end = _addMonths(monthStart, 1)
        label = f"近{n}个月"
        granularity = "month"
    elif unit == "季度":
        quarterStartMonth = ((today.month - 1) // 3) * 3 + 1
        quarterStart = date(today.year, quarterStartMonth, 1)
        start = _addMonths(quarterStart, -3 * (n - 1))
        end = _addMonths(quarterStart, 3)
        label = f"近{n}个季度"
        granularity = "quarter"
    else:
        start = date(today.year - (n - 1), 1, 1)
        end = date(today.year + 1, 1, 1)
        label = f"近{n}年"
        granularity = "year"
    return TimeScope(matched=matched, start=start, end=end, label=label, granularity=granularity)


def _standaloneMonthScope(today: date, offset: int, matched: str) -> TimeScope:
    monthStart = _addMonths(date(today.year, today.month, 1), offset)
    return TimeScope(
        matched=matched,
        start=monthStart,
        end=_addMonths(monthStart, 1),
        label="本月" if offset == 0 else "上月",
        granularity="month",
    )


def _bareYearScope(today: date, offset: int, matched: str) -> TimeScope:
    year = today.year + offset
    return TimeScope(
        matched=matched,
        start=date(year, 1, 1),
        end=date(year + 1, 1, 1),
        label=f"{year}年",
        granularity="year",
    )
```

(e) 偏移常量放**模块级**（正则定义之后，与文件现有常量区风格一致；函数体内不定义 dict）：

```python
# 独立自然月的方向偏移（本月=0，上月=-1）。
_STANDALONE_MONTH_OFFSETS: dict[str, int] = {
    "本": 0, "这": 0, "当前": 0, "这个": 0, "上": -1, "上一": -1, "上个": -1,
}
```

(f) 收集规则（`_collectCandidates` 半年规则之后追加）：

```python
    # 7. 近 N 月/季/年（P4a D3；中文数字先转 int，非法 N 忽略）
    for m in _RE_RECENT_N.finditer(question):
        n = _cnNumberToInt(m.group("n"))
        if n is not None:
            scope = _recentWindowScope(today, n, m.group("unit"), m.group(0))
            if scope is not None:
                add(m, scope)

    # 8. 独立自然月（本月/上月）
    for m in _RE_STANDALONE_MONTH.finditer(question):
        add(m, _standaloneMonthScope(today, _STANDALONE_MONTH_OFFSETS[m.group("dir")], m.group(0)))

    # 9. 裸相对年（今年/去年/前年/明年——不带季度后缀时才落到这里）
    for m in _RE_BARE_YEAR.finditer(question):
        add(m, _bareYearScope(today, _RELATIVE_YEAR_OFFSETS[m.group("rel")], m.group(0)))
```

注意：`_RE_STANDALONE_MONTH` 的 dir 分支顺序保证「上个/这个/上一/当前」先于「上/本/这」命中（regex 交替从左到右优先），否则「这个月」会被「这」+「个月」拆开。

- [ ] **Step 4: 跑 GREEN**

Run: `pytest app/tests/unit/test_time_scope_resolver.py -q`
Expected: 全 PASS（存量 + 新增）。

- [ ] **Step 5: 回归邻近套件**

Run: `pytest app/tests/unit/test_time_scope_threading.py app/tests/unit/test_kpi_time_scope_detection.py -q`
Expected: 全 PASS（检测模块未动；threading 套件覆盖 resolver 并发调用）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/time_scope_resolver.py backend/app/tests/unit/test_time_scope_resolver.py
git commit -m "feat: time_scope_resolver 扩展滚动窗口与自然月/年形态（P4a D3）"
```

---

### Task 2: renderer —— Oracle 渲染 + fail-closed + 组装

**Files:**
- Create: `backend/app/services/time_scope_renderer.py`
- Test: `backend/app/tests/unit/test_time_scope_renderer.py`（新建）

**Interfaces:**
- Consumes: `parseTimeScope`、`TimeScope`（Task 1）；`REASON_EXPLICIT_TIME_SCOPE` 不在此用。
- Produces（Task 4 依赖的精确签名）:

```python
TIME_SCOPE_PLACEHOLDER: str  # "{time_scope}"
REASON_TIME_SCOPE_PARSE_FAILED: str  # "time_scope_parse_failed"
REASON_TIME_SCOPE_RENDER_UNSUPPORTED: str  # "time_scope_render_unsupported_dialect"

class TimeScopeRenderUnsupportedError(Exception): ...

def renderTimeScopePredicate(scope: TimeScope, timeColumn: str, dialect: str) -> str:
    """方言 == oracle ⇒ ANSI DATE 字面量半开区间谓词；其余方言 raise。"""

@dataclass(frozen=True)
class TimeParamFormula:
    ok: bool
    formula: str | None = None
    scopeLabel: str | None = None
    reason: str | None = None

def prepareTimeParameterizedFormula(
    *, formula: str, question: str, timeColumn: str, dialect: str, today: date | None = None
) -> TimeParamFormula: ...
```

- [ ] **Step 1: RED —— 新建测试** `backend/app/tests/unit/test_time_scope_renderer.py`：

```python
"""P4a 时间参数化渲染 —— Oracle DATE 字面量 + 非 Oracle fail-closed（D4 裁决）。"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.time_scope_resolver import parseTimeScope
from app.services.time_scope_renderer import (
    REASON_TIME_SCOPE_PARSE_FAILED,
    REASON_TIME_SCOPE_RENDER_UNSUPPORTED,
    TIME_SCOPE_PLACEHOLDER,
    TimeScopeRenderUnsupportedError,
    prepareTimeParameterizedFormula,
    renderTimeScopePredicate,
)


class TestRenderTimeScopePredicate:
    def test_oracle_half_open_literal_window(self) -> None:
        """整段子句断言（防漂移假绿）：近6个月 ⇒ [2026-06-01, 2026-12-01)。"""
        scope = parseTimeScope("近6个月", today=date(2026, 11, 13))
        predicate = renderTimeScopePredicate(scope, "INVOICE_DATE", "oracle")
        assert predicate == (
            "INVOICE_DATE >= DATE '2026-06-01' AND INVOICE_DATE < DATE '2026-12-01'"
        )

    def test_dialect_is_normalized_case_insensitive(self) -> None:
        scope = parseTimeScope("本月", today=date(2026, 11, 13))
        predicate = renderTimeScopePredicate(scope, "CREATE_TIME", "ORACLE")
        assert predicate == (
            "CREATE_TIME >= DATE '2026-11-01' AND CREATE_TIME < DATE '2026-12-01'"
        )

    @pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
    def test_unsupported_dialect_raises(self, dialect: str) -> None:
        scope = parseTimeScope("近6个月", today=date(2026, 11, 13))
        with pytest.raises(TimeScopeRenderUnsupportedError):
            renderTimeScopePredicate(scope, "INVOICE_DATE", dialect)


class TestPrepareTimeParameterizedFormula:
    def test_placeholder_replaced_with_predicate(self) -> None:
        """ratio 型公式：谓词落 WHERE（聚合前行级过滤，D2 裁决的语义基础）。"""
        formula = (
            "SELECT COUNT(CASE WHEN STATUS = 'OK' THEN 1 END) / NULLIF(COUNT(*), 0) "
            "FROM ORDERS WHERE {time_scope}"
        )
        result = prepareTimeParameterizedFormula(
            formula=formula, question="近6个月准时交付率",
            timeColumn="INVOICE_DATE", dialect="oracle", today=date(2026, 11, 13),
        )
        assert result.ok is True
        assert result.scopeLabel == "近6个月"
        assert TIME_SCOPE_PLACEHOLDER not in (result.formula or "")
        assert (
            "ORDERS WHERE (INVOICE_DATE >= DATE '2026-06-01' "
            "AND INVOICE_DATE < DATE '2026-12-01')" in (result.formula or "")
        )

    def test_parse_failure_reason(self) -> None:
        result = prepareTimeParameterizedFormula(
            formula="SELECT 1 FROM T WHERE {time_scope}",
            question="2026-01-01 到 2026-03-31 的交付率",  # 具体区间不解析（D3）
            timeColumn="INVOICE_DATE", dialect="oracle", today=date(2026, 11, 13),
        )
        assert result.ok is False
        assert result.formula is None
        assert result.reason == REASON_TIME_SCOPE_PARSE_FAILED

    def test_unsupported_dialect_reason(self) -> None:
        result = prepareTimeParameterizedFormula(
            formula="SELECT 1 FROM T WHERE {time_scope}",
            question="近6个月交付率",
            timeColumn="INVOICE_DATE", dialect="postgresql", today=date(2026, 11, 13),
        )
        assert result.ok is False
        assert result.formula is None
        assert result.reason == REASON_TIME_SCOPE_RENDER_UNSUPPORTED

    def test_placeholder_absent_is_idempotent_pass_through(self) -> None:
        """防御：占位符不在 ⇒ 原样放行（路由层本不该调到这里）。"""
        formula = "SELECT COUNT(*) FROM T"
        result = prepareTimeParameterizedFormula(
            formula=formula, question="近6个月交付率",
            timeColumn="INVOICE_DATE", dialect="oracle", today=date(2026, 11, 13),
        )
        assert result.ok is True
        assert result.formula == formula
```

- [ ] **Step 2: 跑 RED**

Run: `pytest app/tests/unit/test_time_scope_renderer.py -q`
Expected: `ModuleNotFoundError: app.services.time_scope_renderer`。

- [ ] **Step 3: GREEN —— 新建 `backend/app/services/time_scope_renderer.py`**：

```python
"""``{time_scope}`` 占位符 → SQL 时间谓词的确定性渲染（P4a，纯函数，无 IO）。

设计（docs/superpowers/specs/2026-11-13-kpi-time-parameterization-design.md §5.2）：
- 用 resolver 算好的半开区间 [start, end) 渲染 **ANSI DATE 字面量**（spec 裁决的
  「等价确定性表达式」）：确定性、可整段子句断言、跨方言同构。
- Oracle 先行（D4）：其余方言抛 ``TimeScopeRenderUnsupportedError``，上层转
  degraded reason，**绝不静默降级 Tier B 让 LLM 猜时间**。
- 谓词是行级过滤，由 formula 作者把占位符写在 WHERE 位置 ⇒ ratio 型公式的
  CASE/NULLIF 天然在聚合前接收过滤（外层包裹做不到这一点，D2 选占位符的原因）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.services.time_scope_resolver import TimeScope, parseTimeScope

TIME_SCOPE_PLACEHOLDER = "{time_scope}"
REASON_TIME_SCOPE_PARSE_FAILED = "time_scope_parse_failed"
REASON_TIME_SCOPE_RENDER_UNSUPPORTED = "time_scope_render_unsupported_dialect"

_SUPPORTED_DIALECTS: frozenset[str] = frozenset({"oracle"})


class TimeScopeRenderUnsupportedError(Exception):
    """方言不支持 ``{time_scope}`` 渲染（fail-closed，设计 D4）。"""


def renderTimeScopePredicate(scope: TimeScope, timeColumn: str, dialect: str) -> str:
    """半开区间 [start, end) → ANSI DATE 字面量谓词；方言不支持则抛错。"""
    normalized = (dialect or "").strip().lower()
    if normalized not in _SUPPORTED_DIALECTS:
        raise TimeScopeRenderUnsupportedError(
            f"dialect {normalized!r} 尚不支持 {{time_scope}} 渲染（P4a 仅 oracle）"
        )
    return (
        f"{timeColumn} >= DATE '{scope.start.isoformat()}' "
        f"AND {timeColumn} < DATE '{scope.end.isoformat()}'"
    )


@dataclass(frozen=True)
class TimeParamFormula:
    """占位符组装结果：ok=False 时 reason 必非空、formula 必为 None。"""

    ok: bool
    formula: str | None = None
    scopeLabel: str | None = None
    reason: str | None = None


def prepareTimeParameterizedFormula(
    *,
    formula: str,
    question: str,
    timeColumn: str,
    dialect: str,
    today: date | None = None,
) -> TimeParamFormula:
    """解析问句时间 ⇒ 渲染谓词 ⇒ 替换 formula 占位符（三步全部确定性）。

    占位符不在 formula ⇒ 原样放行（路由层保证不会走到；幂等防御）。
    """
    if TIME_SCOPE_PLACEHOLDER not in formula:
        return TimeParamFormula(ok=True, formula=formula)

    scope = parseTimeScope(question, today=today)
    if scope is None:
        return TimeParamFormula(ok=False, reason=REASON_TIME_SCOPE_PARSE_FAILED)

    try:
        predicate = renderTimeScopePredicate(scope, timeColumn, dialect)
    except TimeScopeRenderUnsupportedError:
        return TimeParamFormula(ok=False, reason=REASON_TIME_SCOPE_RENDER_UNSUPPORTED)

    return TimeParamFormula(
        ok=True,
        formula=formula.replace(TIME_SCOPE_PLACEHOLDER, f"({predicate})"),
        scopeLabel=scope.label,
    )
```

- [ ] **Step 4: 跑 GREEN**

Run: `pytest app/tests/unit/test_time_scope_renderer.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/time_scope_renderer.py backend/app/tests/unit/test_time_scope_renderer.py
git commit -m "feat: {time_scope} 占位符 Oracle 渲染 + 非 Oracle fail-closed（P4a D4）"
```

---

### Task 3: 路由决策表 —— `time_param_ready` 参数

**Files:**
- Modify: `backend/app/services/kpi_execution_service.py:55-90`
- Modify: `backend/app/services/chat_stream_output.py:404-459`
- Test: `backend/app/tests/unit/test_kpi_execution_service.py`（追加）；`backend/app/tests/unit/test_chat_tier_a_preamble.py`（按需追加）

**Interfaces:**
- Consumes: `TIME_SCOPE_PLACEHOLDER`（Task 2）。
- Produces: `planKpiExecution(..., time_param_ready: bool = False)`——`has_explicit_time_scope` 且 `time_param_ready` 时**不再**短路 Tier B，继续走 formula/tier_a_eligible 短路链。Task 4 依赖此行为。

- [ ] **Step 1: RED —— 决策表用例**（追加到 `test_kpi_execution_service.py`）：

```python
# =============================================================================
# P4a：time_param_ready —— 显式时间 + 占位符就绪 ⇒ 不再一票降级 Tier B
# =============================================================================


def test_explicit_time_without_placeholder_still_tier_b() -> None:
    """占位符缺失 ⇒ 行为零变化（现状回归）。"""
    plan = planKpiExecution(
        switch_on=True, kpi_found=True, metric_present=True,
        execution_source="ESCAPE_HATCH", tier_a_eligible=True, has_datasource=True,
        formula_present=True, has_explicit_time_scope=True, time_param_ready=False,
    )
    assert (plan.tier, plan.reason) == ("B", "explicit_time_scope")


def test_explicit_time_with_placeholder_reaches_tier_a() -> None:
    plan = planKpiExecution(
        switch_on=True, kpi_found=True, metric_present=True,
        execution_source="ESCAPE_HATCH", tier_a_eligible=True, has_datasource=True,
        formula_present=True, has_explicit_time_scope=True, time_param_ready=True,
    )
    assert (plan.tier, plan.reason) == ("A", None)


def test_placeholder_ready_still_respects_later_short_circuits() -> None:
    """占位符就绪不能跳过后续短路（对账不合格仍 Tier B）。"""
    plan = planKpiExecution(
        switch_on=True, kpi_found=True, metric_present=True,
        execution_source="ESCAPE_HATCH", tier_a_eligible=False, has_datasource=True,
        formula_present=True, has_explicit_time_scope=True, time_param_ready=True,
    )
    assert (plan.tier, plan.reason) == ("B", "reconcile_not_eligible")


def test_time_param_ready_defaults_false() -> None:
    """不传新参的旧调用行为不变（向后兼容）。"""
    plan = planKpiExecution(
        switch_on=True, kpi_found=True, metric_present=True,
        execution_source="ESCAPE_HATCH", tier_a_eligible=True, has_datasource=True,
        formula_present=True, has_explicit_time_scope=True,
    )
    assert plan.reason == "explicit_time_scope"
```

- [ ] **Step 2: 跑 RED**

Run: `pytest app/tests/unit/test_kpi_execution_service.py -q`
Expected: 新用例 FAIL（`TypeError: unexpected keyword argument 'time_param_ready'`），存量 PASS。

- [ ] **Step 3: GREEN —— 决策表改一处**（`kpi_execution_service.py`）：

签名追加 `time_param_ready: bool = False`（放 `has_explicit_time_scope` 之后），:84 改为：

```python
    # P4a：formula 声明 {time_scope} 且 time_column 就绪时，显式时间问句不再
    # 一票降级 Tier B（渲染失败在执行层降级，见 time_scope_renderer）。
    if has_explicit_time_scope and not time_param_ready:
        return KpiExecutionPlan(TIER_B, REASON_EXPLICIT_TIME_SCOPE)
```

- [ ] **Step 4: GREEN —— `_planKpiTier` 传参**（`chat_stream_output.py`）：

(a) 顶部 import 补 `TIME_SCOPE_PLACEHOLDER`（from `app.services.time_scope_renderer`）。

(b) 三个 `planKpiExecution` 调用点全部追加 `time_param_ready=...`：
- 开关关分支（:413）与 kpi None 分支（:419）：`time_param_ready=False`（不碰 kpi 属性的既有注释约定保持）；
- 正常分支（:440）改为：

```python
        # P4a：占位符 + time_column 双就绪才允许显式时间问句走 Tier A；
        # 二者任一缺失 ⇒ 维持 explicit_time_scope 降级（行为零变化）。
        timeParamReady = bool(
            metric is not None
            and TIME_SCOPE_PLACEHOLDER in (metric.formula or "")
            and (metric.time_column or "").strip()
        )
        plan = planKpiExecution(
            switch_on=True,
            kpi_found=True,
            metric_present=metric is not None,
            execution_source=(metric.execution_source if metric is not None else None),
            tier_a_eligible=bool(reconcile is not None and reconcile.isTierAEligible),
            has_datasource=kpi.datasource_id is not None,
            formula_present=bool(metric is not None and (metric.formula or "").strip()),
            has_explicit_time_scope=hasExplicitTime,
            time_param_ready=timeParamReady,
        )
```

- [ ] **Step 5: 跑 GREEN + 回归**

Run: `pytest app/tests/unit/test_kpi_execution_service.py app/tests/unit/test_chat_tier_a_preamble.py app/tests/unit/test_kpi_execution_plan.py -q`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/kpi_execution_service.py backend/app/services/chat_stream_output.py backend/app/tests/unit/test_kpi_execution_service.py
git commit -m "feat: 路由决策表加 time_param_ready（显式时间 + 占位符就绪走 Tier A）"
```

---

### Task 4: 执行层接入 —— `_executeTierA` 渲染占位符

**Files:**
- Modify: `backend/app/services/chat_service.py:1589-1638`（`_executeTierA` 签名 + 渲染）
- Modify: `backend/app/services/chat_stream_output.py:461-469`（占位实现签名）与 :595（调用点）
- Modify: `backend/app/tests/unit/test_chat_tier_a_preamble.py`（桩同步）
- Modify: `backend/app/tests/integration/test_kpi_tier_a_chat_api.py`（`_seedKpiWithMetric` 加参数 + 两个新用例）

**Interfaces:**
- Consumes: `prepareTimeParameterizedFormula`、`TIME_SCOPE_PLACEHOLDER`（Task 2）；`time_param_ready` 路由（Task 3）。
- Produces: `_executeTierA(self, match, kpi, session, question: str | None = None)`——**keyword 传参**（`question=...`），旧桩必须同步加参否则 `TypeError`。

测试分层依据（recon 实证）：`ChatService._executeTierA` 依赖 session/adapter，单元桩成本高；渲染纯函数已由 Task 2 全覆盖，本任务的真实证据走**集成层**（`test_kpi_tier_a_chat_api.py`：真实 PG + HTTP 链路，业务库=测试元数据库自身；oracle 用例 monkeypatch `get_adapter` 捕获 formula）。

- [ ] **Step 1: RED —— 桩同步（单元）+ 两个集成用例**

(a) `test_chat_tier_a_preamble.py` 两处桩（:65、:176）签名改为 `async def _executeTierA(self, match, kpi, session, question=None):  # noqa: ANN001`；:194 直调保持 3 参（默认值兼容）。跑一遍确认存量绿（桩同步本身无新断言）。

(b) `test_kpi_tier_a_chat_api.py`：`_seedKpiWithMetric`（:120）签名加 `timeColumn: str | None = None`，`OntologyMetric(...)` 构造里传 `time_column=timeColumn`（其余不变，向后兼容）。

(c) 追加两个集成用例（文件末尾；夹具/模式与既有 §7 用例逐一同款）：

```python
# ---------------------------------------------------------------------------
# 8. P4a：{time_scope} 占位符 —— 非 Oracle fail-closed / Oracle 渲染放行
# ---------------------------------------------------------------------------

_PLACEHOLDER_FORMULA = "SELECT COUNT(*) AS cnt FROM kpi_catalog WHERE {time_scope}"


async def test_placeholder_metric_pg_fails_closed_to_tier_b(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """占位符 + time_column 就绪 + postgresql ⇒ 执行层渲染 fail-closed。

    路由判 Tier A（time_param_ready=True），渲染层抛 unsupported ⇒ degraded
    reason=time_scope_render_unsupported_dialect；绝不让 LLM 猜时间、绝不 500。
    """
    ds = await _seedSelfDatasource(dbSession)  # type == "postgresql"
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession)
    await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TScopePG",
        kpiName="时间占位PG指标",
        keywords=["时间占位PG指标"],
        formula=_PLACEHOLDER_FORMULA,
        timeColumn="created_time",
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL,
        json=_payload("近6个月时间占位PG指标", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executionTier"] == "B"
    assert body["degradedReason"] == "time_scope_render_unsupported_dialect"


async def test_placeholder_metric_oracle_renders_predicate_into_formula(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """占位符 + oracle ⇒ 渲染成功，发给 adapter 的 formula 含 DATE 字面量谓词。

    本地无真实 Oracle：monkeypatch ``get_adapter`` 捕获执行入参（渲染层证据），
    fake adapter 返回合法行 ⇒ 全链路判 Tier A。
    """
    captured: dict[str, str] = {}

    class _FakeAdapter:
        async def execute_read_only(self, sql: str, maxRows: int):  # noqa: ANN001
            captured["sql"] = sql
            return [{"cnt": 7}]

    async def _fakeGetAdapter(datasourceId, datasource):  # noqa: ANN001
        return _FakeAdapter()

    import app.services.kpi_execution_service as _execSvc

    monkeypatch.setattr(_execSvc, "get_adapter", _fakeGetAdapter)

    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession)
    metric, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TScopeOra",
        kpiName="时间占位Oracle指标",
        keywords=["时间占位Oracle指标"],
        formula=_PLACEHOLDER_FORMULA,
        timeColumn="created_time",
    )
    # 自连 datasource 是 postgresql ⇒ 改 type 后 flush（本用例专测 oracle 渲染分支）。
    ds.type = "oracle"
    await dbSession.flush()
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL,
        json=_payload("近6个月时间占位Oracle指标", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executionTier"] == "A", body
    sql = captured["sql"]
    assert "{time_scope}" not in sql
    assert "created_time >= DATE '" in sql
    assert "created_time < DATE '" in sql
```

（`_payload`/`_warmKpiCache`/`_installTierBFakes` 为该文件既有 helper，直接复用；`ds.type = "oracle"` 后 flush 是测试夹具内的合法变更——monkeypatch 掉 adapter 后无真实连接。）

- [ ] **Step 2: 跑 RED**

Run: `pytest app/tests/unit/test_chat_tier_a_preamble.py -q`
Expected: 新用例 FAIL（当前实现无渲染路径，占位符 formula 原样进 execute）。

- [ ] **Step 3: GREEN —— `chat_service.py`**：

(a) import 补 `prepareTimeParameterizedFormula`、`TIME_SCOPE_PLACEHOLDER`、`REASON_TIME_SCOPE_PARSE_FAILED`（from `app.services.time_scope_renderer`）。

(b) `chat_stream_output.py:461` 占位实现签名同步加 `question: str | None = None`（实现体不变）。

(c) `chat_stream_output.py:595` 调用点改：

```python
                        l1, degradedReason = await self._executeTierA(
                            match, kpi, session, question=dto.question
                        )
```

(d) `chat_service.py` `_executeTierA`（:1589）签名加 `question: str | None = None`，查完 metric/datasource 之后、`execute` 之前插入：

```python
        # P4a：占位符渲染（确定性三步：解析 → 渲染 → 替换）。
        # 渲染失败 ⇒ (None, reason) 走 Tier B 哨兵；绝不能把含占位符原文的
        # formula 发给数据库（花括号必语法错，且语义不可控）。
        formula = metric.formula or ""
        timeColumn = (metric.time_column or "").strip()
        if TIME_SCOPE_PLACEHOLDER in formula:
            if not timeColumn or not (question or "").strip():
                return None, REASON_TIME_SCOPE_PARSE_FAILED
            prepared = prepareTimeParameterizedFormula(
                formula=formula,
                question=question,
                timeColumn=timeColumn,
                dialect=datasource.type.value if hasattr(datasource.type, "value") else str(datasource.type),
            )
            if not prepared.ok:
                logger.warning(
                    "TierA 时间参数化失败 kpi=%s reason=%s", match.code, prepared.reason
                )
                return None, prepared.reason
            formula = prepared.formula or formula
```

原 `execute(formula=metric.formula or "", ...)` 改为 `execute(formula=formula, ...)`（其余参数不动）。

⚠️ 测试缺口注记：`question is None`（或空）+ formula 含占位符的防御分支无自动化用例（集成链路 question 恒非空、单元层构造 ChatService 成本过高）——该分支纳入 task review 检查项（评审时人工核对三分支：无 timeColumn / 无 question / prepare 失败，全部 `return None, ...` 且不触达 execute）。

- [ ] **Step 4: 跑 GREEN + 回归**

Run: `pytest app/tests/unit/test_chat_tier_a_preamble.py app/tests/integration/test_kpi_tier_a_chat_api.py app/tests/unit/test_kpi_execution_service.py app/tests/unit/test_time_scope_renderer.py -q`
Expected: 全 PASS。若两个新集成用例红，先核对 `_seedKpiWithMetric` 的 `timeColumn` 参数与 `ds.type` 变更是否落盘（flush 时机），再核对 monkeypatch 目标（`app.services.kpi_execution_service.get_adapter`——模块级 from-import，patch 定义处）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat_service.py backend/app/services/chat_stream_output.py backend/app/tests/unit/test_chat_tier_a_preamble.py backend/app/tests/integration/test_kpi_tier_a_chat_api.py
git commit -m "feat: Tier A 执行层渲染 {time_scope} 占位符，渲染失败降级 Tier B"
```

---

### Task 5: 治理校验 —— 占位符 ⇔ time_column

**Files:**
- Modify: `backend/app/services/ontology_service.py`（`createMetric` :908 / `updateMetric` :1022）
- Modify: `backend/app/services/messages_zh.py`（新消息常量）
- Test: `backend/app/tests/integration/test_ontology_metric_write_gate.py`（追加）

**Interfaces:**
- Consumes: `TIME_SCOPE_PLACEHOLDER`（Task 2）；`ValidationError`（⇒ HTTP 422，`statusForError` 唯一映射源）。
- Produces: 校验仅服务层内部行为，无新接口。

- [ ] **Step 1: RED —— 集成用例**（追加到 `test_ontology_metric_write_gate.py`，沿用 `ADMIN_HEADERS`/`client` 夹具）：

```python
# =============================================================================
# P4a：formula 含 {time_scope} 占位符 ⇒ time_column 必填（反向不强制）
# =============================================================================


async def test_create_metric_with_placeholder_without_time_column_rejected(client: AsyncClient) -> None:
    payload = {
        **_METRIC_PAYLOAD,
        "formula": "SELECT COUNT(*) FROM ORDERS WHERE {time_scope}",
        "executionSource": "ESCAPE_HATCH",
        # 无 timeColumn
    }
    resp = await client.post("/api/v1/ontology/metrics", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 422, resp.text


async def test_create_metric_with_placeholder_and_time_column_ok(client: AsyncClient) -> None:
    payload = {
        **_METRIC_PAYLOAD,
        "formula": "SELECT COUNT(*) FROM ORDERS WHERE {time_scope}",
        "executionSource": "ESCAPE_HATCH",
        "timeColumn": "INVOICE_DATE",
    }
    resp = await client.post("/api/v1/ontology/metrics", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 201, resp.text


async def test_update_metric_to_placeholder_without_column_rejected(client: AsyncClient) -> None:
    """partial update：formula 改为含占位符、time_column 未传且原值空 ⇒ 422。"""
    metric_id = await _createMetric(client)  # 现有 helper，formula 无占位符
    resp = await client.put(
        f"/api/v1/ontology/metrics/{metric_id}",
        json={"formula": "SELECT COUNT(*) FROM ORDERS WHERE {time_scope}"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422, resp.text


async def test_update_metric_placeholder_with_existing_column_ok(client: AsyncClient) -> None:
    """先建（占位符+列），再 update 别的合法字段 ⇒ 正常（列已满足约束）。"""
    payload = {
        **_METRIC_PAYLOAD,
        "formula": "SELECT COUNT(*) FROM ORDERS WHERE {time_scope}",
        "executionSource": "ESCAPE_HATCH",
        "timeColumn": "INVOICE_DATE",
    }
    resp = await client.post("/api/v1/ontology/metrics", json=payload, headers=ADMIN_HEADERS)
    metric_id = resp.json()["id"]
    resp = await client.put(
        f"/api/v1/ontology/metrics/{metric_id}",
        json={"metricAlias": "别名更新"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
```

（键名已核实：`OntologyMetricCreate`（`app/domain/schemas.py:685`）/ `OntologyMetricUpdate`（:719）字段为 `execution_source`/`time_column`，CamelModel ⇒ JSON 键 `executionSource`/`timeColumn`。`updateMetric` 的 DTO 无 `time_grain` 之外的时间列校验冲突。）

- [ ] **Step 2: 跑 RED**

Run: `pytest app/tests/integration/test_ontology_metric_write_gate.py -q`
Expected: 新用例 FAIL（201/200 而非 422），存量 PASS。

- [ ] **Step 3: GREEN**：

(a) `messages_zh.py` 追加（**避免字面花括号**，防 `.format` 转义雷）：

```python
MSG_METRIC_TIME_SCOPE_PLACEHOLDER_REQUIRES_COLUMN = (
    "指标 formula 声明了时间占位符（time_scope）时必须同时配置 time_column（时间列）"
)
```

(b) `ontology_service.py` 顶部（import 区）补 `TIME_SCOPE_PLACEHOLDER`；`createMetric` 构造 entity 之前加：

```python
        # P4a：占位符 ⇔ time_column 一致性（写入校验，spec §5.4）。
        _assertTimeScopePlaceholderConsistency(
            formula=dto.formula, timeColumn=dto.time_column
        )
```

(c) `updateMetric` 在 P1 发布闸门调用之前加：

```python
        # P4a：合并后的 formula/time_column 一致性（partial update 用原值补位）。
        _assertTimeScopePlaceholderConsistency(
            formula=updates.get("formula", entity.formula),
            timeColumn=updates.get("time_column", entity.time_column),
        )
```

(d) 模块内（`createMetric` 之前）加纯函数：

```python
def _assertTimeScopePlaceholderConsistency(
    *, formula: str | None, timeColumn: str | None
) -> None:
    """P4a 写入校验：formula 含时间占位符 ⇒ time_column 必填（反向不强制）。"""
    if TIME_SCOPE_PLACEHOLDER in (formula or "") and not (timeColumn or "").strip():
        raise ValidationError(MSG_METRIC_TIME_SCOPE_PLACEHOLDER_REQUIRES_COLUMN)
```

（import `ValidationError` from `app.domain.exceptions`——该文件若已有则复用。）

- [ ] **Step 4: 跑 GREEN**

Run: `pytest app/tests/integration/test_ontology_metric_write_gate.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ontology_service.py backend/app/services/messages_zh.py backend/app/tests/integration/test_ontology_metric_write_gate.py
git commit -m "feat: 指标 formula 时间占位符 ⇔ time_column 写入校验（P4a）"
```

---

### Task 6: 存量改写脚本 —— dry-run 建议（只读）

**Files:**
- Create: `backend/app/services/kpi_time_scope_rewrite.py`（纯函数）
- Create: `backend/scripts/kpi_rewrite_time_scope.py`（CLI，只读）
- Test: `backend/app/tests/unit/test_kpi_time_scope_rewrite.py`（新建）

**Interfaces:**
- Consumes: `TIME_SCOPE_PLACEHOLDER`（Task 2）。
- Produces:

```python
ACTION_NOOP = "noop"          # formula 已含占位符
ACTION_SUGGEST = "suggest"    # time_column 就绪 ⇒ 给出建议改写文本
ACTION_BLOCKED = "blocked"    # time_column 缺失 ⇒ 先治理补列，不猜插入位置

@dataclass(frozen=True)
class TimeScopeRewriteSuggestion:
    metricId: int
    metricName: str
    executionSource: str | None
    formula: str
    timeColumn: str | None
    action: str
    reason: str | None
    suggestedFormula: str | None

def buildTimeScopeRewriteSuggestions(
    metrics: Sequence[Mapping[str, object]],
) -> tuple[TimeScopeRewriteSuggestion, ...]: ...
```

CLI：`python -m scripts.kpi_rewrite_time_scope`——只读 SELECT `ontology_metric`，stdout 表格 + `--output report.json` 可选；**无 --apply 模式**（写入走 admin 表单，走既有审计）。

- [ ] **Step 1: RED —— 纯函数用例**（新建 `test_kpi_time_scope_rewrite.py`）：

```python
"""P4a 存量改写建议（纯函数 dry-run，零 IO 零写入）。"""

from __future__ import annotations

from app.services.kpi_time_scope_rewrite import (
    ACTION_BLOCKED,
    ACTION_NOOP,
    ACTION_SUGGEST,
    TIME_SCOPE_PLACEHOLDER,
    buildTimeScopeRewriteSuggestions,
)


def _metric(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 2,
        "metric_name": "准时交付率",
        "execution_source": "ESCAPE_HATCH",
        "formula": "SELECT COUNT(CASE WHEN STATUS='OK' THEN 1 END)/NULLIF(COUNT(*),0) FROM ORDERS",
        "time_column": None,
        "status": "PUBLISHED",
    }
    base.update(overrides)
    return base


def test_no_placeholder_and_no_column_is_blocked() -> None:
    """R3 不猜：列缺失不给建议文本，报告 blocked。"""
    (s,) = buildTimeScopeRewriteSuggestions([_metric()])
    assert s.action == ACTION_BLOCKED
    assert s.suggestedFormula is None
    assert s.reason is not None


def test_with_column_appends_where_clause() -> None:
    """无 WHERE 的聚合公式 ⇒ 追加 ` WHERE ({占位符})`（行级过滤在聚合前）。"""
    (s,) = buildTimeScopeRewriteSuggestions(
        [_metric(time_column="INVOICE_DATE")]
    )
    assert s.action == ACTION_SUGGEST
    assert s.suggestedFormula == (
        "SELECT COUNT(CASE WHEN STATUS='OK' THEN 1 END)/NULLIF(COUNT(*),0) FROM ORDERS"
        f" WHERE ({TIME_SCOPE_PLACEHOLDER})"
    )


def test_with_column_and_where_inserts_after_first_where() -> None:
    (s,) = buildTimeScopeRewriteSuggestions(
        [_metric(time_column="INVOICE_DATE",
                 formula="SELECT COUNT(*) FROM ORDERS WHERE STATUS='OK'")]
    )
    assert s.action == ACTION_SUGGEST
    assert s.suggestedFormula == (
        f"SELECT COUNT(*) FROM ORDERS WHERE ({TIME_SCOPE_PLACEHOLDER}) AND STATUS='OK'"
    )


def test_existing_placeholder_is_noop() -> None:
    (s,) = buildTimeScopeRewriteSuggestions(
        [_metric(time_column="INVOICE_DATE",
                 formula=f"SELECT COUNT(*) FROM T WHERE ({TIME_SCOPE_PLACEHOLDER})")]
    )
    assert s.action == ACTION_NOOP
    assert s.suggestedFormula is None


def test_non_escape_hatch_metrics_are_excluded() -> None:
    suggestions = buildTimeScopeRewriteSuggestions(
        [_metric(execution_source="COMPILED", time_column="INVOICE_DATE")]
    )
    assert suggestions == ()
```

- [ ] **Step 2: 跑 RED**

Run: `pytest app/tests/unit/test_kpi_time_scope_rewrite.py -q`
Expected: `ModuleNotFoundError`。

- [ ] **Step 3: GREEN —— 纯函数**（`backend/app/services/kpi_time_scope_rewrite.py`）：

```python
"""存量 ESCAPE_HATCH 公式的 ``{time_scope}`` 改写建议（纯函数，零 IO）。

D5 裁决：脚本只产 dry-run 建议，不写库；admin 按建议在指标编辑表单逐条
确认改写（走既有审计）。time_column 缺失 ⇒ blocked（R3 不猜列名）。
插入启发式（建议文本仍需 admin 人工确认）：
- formula 无 WHERE ⇒ 追加 ` WHERE ({占位符})`；
- 有 WHERE ⇒ 第一个 WHERE 后插 `({占位符}) AND `。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.services.time_scope_renderer import TIME_SCOPE_PLACEHOLDER

ACTION_NOOP = "noop"
ACTION_SUGGEST = "suggest"
ACTION_BLOCKED = "blocked"

_RE_FIRST_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_PLACEHOLDER_IN_PARENS = f"({TIME_SCOPE_PLACEHOLDER})"


@dataclass(frozen=True)
class TimeScopeRewriteSuggestion:
    metricId: int
    metricName: str
    executionSource: str | None
    formula: str
    timeColumn: str | None
    action: str
    reason: str | None
    suggestedFormula: str | None


def buildTimeScopeRewriteSuggestions(
    metrics: Sequence[Mapping[str, object]],
) -> tuple[TimeScopeRewriteSuggestion, ...]:
    """ESCAPE_HATCH + formula 非空才参与；其余输入原样跳过。"""
    suggestions: list[TimeScopeRewriteSuggestion] = []
    for row in metrics:
        executionSource = (row.get("execution_source") or "").strip().upper() or None
        if executionSource != "ESCAPE_HATCH":
            continue
        formula = (row.get("formula") or "").strip()
        if not formula:
            continue
        timeColumn = (row.get("time_column") or "").strip() or None
        metricId = int(row["id"])
        metricName = str(row.get("metric_name") or "")
        if TIME_SCOPE_PLACEHOLDER in formula:
            suggestions.append(TimeScopeRewriteSuggestion(
                metricId=metricId, metricName=metricName,
                executionSource=executionSource, formula=formula,
                timeColumn=timeColumn, action=ACTION_NOOP,
                reason=None, suggestedFormula=None,
            ))
            continue
        if timeColumn is None:
            suggestions.append(TimeScopeRewriteSuggestion(
                metricId=metricId, metricName=metricName,
                executionSource=executionSource, formula=formula,
                timeColumn=None, action=ACTION_BLOCKED,
                reason="time_column 缺失：先治理补列（R3 不猜），再改写占位符",
                suggestedFormula=None,
            ))
            continue
        match = _RE_FIRST_WHERE.search(formula)
        if match:
            insertAt = match.end()
            suggested = (
                formula[:insertAt] + f" {_PLACEHOLDER_IN_PARENS} AND" + formula[insertAt:]
            )
            reason = "第一个 WHERE 后插入占位符（请人工确认不破坏既有条件语义）"
        else:
            suggested = f"{formula} WHERE {_PLACEHOLDER_IN_PARENS}"
            reason = "formula 无 WHERE，末尾追加（行级过滤在聚合前，ratio 语义安全）"
        suggestions.append(TimeScopeRewriteSuggestion(
            metricId=metricId, metricName=metricName,
            executionSource=executionSource, formula=formula,
            timeColumn=timeColumn, action=ACTION_SUGGEST,
            reason=reason, suggestedFormula=suggested,
        ))
    return tuple(suggestions)
```

- [ ] **Step 4: 跑 GREEN**

Run: `pytest app/tests/unit/test_kpi_time_scope_rewrite.py -q`
Expected: 全 PASS。

- [ ] **Step 5: GREEN —— CLI**（`backend/scripts/kpi_rewrite_time_scope.py`，骨架照 `scripts/kpi_backfill_time_grain.py` 实测模式：`sys.path.insert` + `create_async_engine` + 目标守卫；**只读、无 --apply**）：

```python
"""P4a 存量改写 dry-run 报告（只读；无 --apply 模式）。

建议规则（D5 裁决）在 :mod:`app.services.kpi_time_scope_rewrite`（纯函数）；
本脚本只负责读库、打印报告。写入一律走 admin 指标编辑表单（走既有审计）。

用法::

    python3 scripts/kpi_rewrite_time_scope.py \
        --database-url 'postgresql+asyncpg://.../qa_metadata'   # dry-run 报告
    python3 scripts/kpi_rewrite_time_scope.py --database-url ... --output report.json

退出码：0 正常；1 参数/连接失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 直接 `python3 scripts/xxx.py` 运行时 sys.path[0] 是 scripts/ ⇒ `app` 不可导入
# （见 kpi_backfill_time_grain.py 同款注释：editable 安装的旧副本遮蔽陷阱）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.services.kpi_time_scope_rewrite import buildTimeScopeRewriteSuggestions  # noqa: E402
from scripts.kpi_metric_db_guard import assertSafeTarget, maskDsn  # noqa: E402

_SELECT_METRICS = (
    "SELECT id, metric_name, execution_source, formula, time_column, status "
    "FROM ontology_metric ORDER BY id"
)


async def _run(databaseUrl: str, outputPath: str | None) -> int:
    print(f"目标库: {maskDsn(databaseUrl)}")
    assertSafeTarget(databaseUrl)
    engine = create_async_engine(databaseUrl)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(_SELECT_METRICS))).mappings().all()
    finally:
        await engine.dispose()
    suggestions = buildTimeScopeRewriteSuggestions(list(rows))
    counts: dict[str, int] = {}
    for s in suggestions:
        counts[s.action] = counts.get(s.action, 0) + 1
        print(f"[{s.action.upper():7}] metric={s.metricId} {s.metricName} col={s.timeColumn}")
        if s.suggestedFormula:
            print(f"    建议: {s.suggestedFormula}")
        if s.reason:
            print(f"    说明: {s.reason}")
    print(f"\n汇总: {counts}（共 {len(suggestions)} 条 ESCAPE_HATCH 指标；dry-run 零写入）")
    if outputPath:
        with open(outputPath, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "metricId": s.metricId, "metricName": s.metricName,
                        "executionSource": s.executionSource, "formula": s.formula,
                        "timeColumn": s.timeColumn, "action": s.action,
                        "reason": s.reason, "suggestedFormula": s.suggestedFormula,
                    }
                    for s in suggestions
                ],
                fh, ensure_ascii=False, indent=2,
            )
        print(f"报告已写入 {outputPath}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="目标库 DSN（只读连接）")
    parser.add_argument("--output", default=None, help="JSON 报告输出路径")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args.database_url, args.output))
    except Exception as exc:  # noqa: BLE001 — CLI 边界，显式报错退出
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

（`assertSafeTarget`/`maskDsn` 的实际签名以 `scripts/kpi_metric_db_guard.py` 为准——实现者先读它；若 `assertSafeTarget` 只拦非 dev 库而 dry-run 只读无害，按其现状用法调用。）

- [ ] **Step 6: 验证 CLI dry-run（dev 5433，只读）**

Run: `cd /private/tmp/qa-kpi-p4a/backend && python3 scripts/kpi_rewrite_time_scope.py --database-url 'postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5433/qa_metadata'`
Expected: 报告 8 条 ESCAPE_HATCH 指标（metric id 2-9）；time_column 缺失的 7 条 `BLOCKED`，metric 5 `SUGGEST`（无写副作用）。

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/kpi_time_scope_rewrite.py backend/scripts/kpi_rewrite_time_scope.py backend/app/tests/unit/test_kpi_time_scope_rewrite.py
git commit -m "feat: {time_scope} 存量改写 dry-run 建议（只读脚本 + 纯函数）"
```

---

### Task 7: 定向回归 + 变更记录

**Files:**
- Create: `Harness/changes/2026-11-13-kpi-p4a-time-parameterization/summary.md`

- [ ] **Step 1: 定向回归**（一次命令，全绿才算过）：

```bash
cd /private/tmp/qa-kpi-p4a/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/unit/test_time_scope_resolver.py \
       app/tests/unit/test_time_scope_renderer.py \
       app/tests/unit/test_kpi_time_scope_rewrite.py \
       app/tests/unit/test_kpi_execution_service.py \
       app/tests/unit/test_kpi_execution_plan.py \
       app/tests/unit/test_chat_tier_a_preamble.py \
       app/tests/unit/test_time_scope_threading.py \
       app/tests/unit/test_kpi_time_scope_detection.py \
       app/tests/integration/test_ontology_metric_write_gate.py \
       app/tests/integration/test_kpi_tier_a_chat_api.py -q
```
Expected: 全 PASS。若出现与本批无关的既有基线红，按「净新增失败 0」判据三角定位（同码本地通过 + diff 不涉及 + 跨 run 同因）。

- [ ] **Step 2: 邻近 chat 套件抽查**（改了 chat_stream_output/chat_service，须核对既有降级路径未被破坏）：

```bash
pytest app/tests/unit/test_kpi_tier_a_probe.py app/tests/unit/test_kpi_metric_tier_a_switch.py app/tests/unit/test_kpi_tier_parity.py -q
```
Expected: 全 PASS。

- [ ] **Step 3: 变更记录**（按 `Harness/changes/2026-11-11-kpi-p3-alias-resolver/summary.md` 格式）：背景 / 决策 D1-D5 / 变更清单（7 commit 对应）/ 验收对照（spec §8 六条，Oracle 端到端人工冒烟注明「待发版后执行」）/ 明确不做（日期区间、P4b、PG/MySQL、week）。

- [ ] **Step 4: Commit**

```bash
git add Harness/changes/2026-11-13-kpi-p4a-time-parameterization/summary.md
git commit -m "docs: KPI P4a 时间参数化变更记录"
```

---

## 文件清单

| 文件 | 动作 |
|---|---|
| `backend/app/services/time_scope_resolver.py` | 扩展（滚动窗口 + 自然月/年 + 中文数字；docstring 旧约束改判） |
| `backend/app/services/time_scope_renderer.py` | 新建（Oracle 渲染 + fail-closed + 组装） |
| `backend/app/services/kpi_execution_service.py` | `planKpiExecution` 加 `time_param_ready` |
| `backend/app/services/chat_stream_output.py` | `_planKpiTier` 传参 + 占位实现签名 + 调用点 keyword |
| `backend/app/services/chat_service.py` | `_executeTierA` 渲染接入 |
| `backend/app/services/ontology_service.py` | 写入校验（create/update） |
| `backend/app/services/messages_zh.py` | 1 条消息常量 |
| `backend/app/services/kpi_time_scope_rewrite.py` | 新建（建议纯函数） |
| `backend/scripts/kpi_rewrite_time_scope.py` | 新建（CLI 只读 dry-run） |
| 测试 5 个文件（2 新建 3 追加） | 见各任务 |
| `Harness/changes/2026-11-13-kpi-p4a-time-parameterization/summary.md` | 新建 |
| **无 Alembic 迁移、无 DB DDL** | — |

## 风险与回滚

| 风险 | 处置 |
|---|---|
| 新 resolver 形态误伤旧形态（「今年一季度」落裸年） | Task 1 交互守卫用例（长匹配优先）+ 全量 resolver 回归 |
| 占位符原文发给 DB | Task 4 三重防御（渲染失败/无 question/无 time_column ⇒ 一律 return，永不 execute 原文） |
| 桩签名不兼容炸存量测试 | keyword 传参 + 默认值；Task 4 Step 1 先改桩（RED 前置） |
| 集成用例 monkeypatch 目标错（patch 使用处而非定义处） | patch `app.services.kpi_execution_service.get_adapter`（from-import 定义处）；Task 4 Step 4 排错指引已写 |
| CLI 目标守卫签名不符 | Task 6 注明先读 `scripts/kpi_metric_db_guard.py`；dry-run 只读无害 |
| 回滚 | 逐 commit revert；无迁移无 DDL；占位符是治理数据（formula 改回即关闭该指标参数化） |
