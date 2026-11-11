# P2 Tier A 确定性执行器 — 逐日实施计划

> 分支 `feat/kpi-tier-a-executor`，worktree `/private/tmp/qa-kpi-p2`，base = `origin/main`（`00f3b13e`）。
> 设计稿：`docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md` §6 / §9 / §12 / §13-M2。
> 实施方式：逐日 TDD（RED → GREEN → IMPROVE），每天结束跑当天定向测试；D7 做整批回归 + 双重评审。
> **本批零 DDL、零生产写操作、零部署；不创建任何 `system_config` 行。**

## 0. Definition of Done

1. `KPI_METRIC_TIER_A_ENABLED` 双闸（env 杀闸 + `system_config` 灰度行）**fail-closed**：行缺失/非法/读异常 ⇒ 关；关 ⇒ 旧行为逐字不变。
2. `execution_source=ESCAPE_HATCH` 的 KPI：唯一命中 ⇒ 服务端执行 `metric.formula`，返回真实数值，`tokensUsed=0`、`cost=0.0`。
3. `COMPILED` ⇒ Tier B（`degraded_reason="compiled_not_supported"`）；`NONE`/空 ⇒ Tier C 文案；未知值 ⇒ Tier C + warning。
4. 执行异常/超时/空结果 ⇒ **不静默**：warning 带 `kpi_code`/`datasource_id`/cause 链，落到 Tier B 且响应带 `degradedReason`。
5. 问句含显式时间限定 ⇒ 不执行 formula，直接 Tier B（`degraded_reason="explicit_time_scope"`）。
6. 只读三重保险：`_assert_read_only`（执行器）→ adapter 内部再验 → 库侧只读事务；非 SELECT/DDL/DML 必须抛 `SqlSafetyError` 且 **adapter 一次都不调用**。
7. 结果 ≤ 1000 行，超出截断并标记 `truncated`；单值取首行第一个非 None。
8. `ChatResponse` 新增 `executionTier` / `degradedReason`（A/B/C/LEGACY）；非流式 L1 路径与 Tier B 路径都带值；流式 meta 帧带值。
9. 证据：Tier A 成功执行调度 `METRIC_RESULT`（best-effort，失败只 warning）；adapter 既有 `SQL_QUERY` 证据不重复实现。
10. 覆盖率 ≥ 80%（新模块行覆盖）；定向测试全绿；净新增失败 = 0（失败集合对差）。

**明确不做**：不改生产库；不建/不删 `system_config` 行；不改 alembic；不动 P3/P4（别名解析、参数化编译器）；不重构 `kpi_contract_builder`；不新增前端字段渲染（仅透传 API 字段）。

**设计稿两处口径的落地解释**（避免评审时当成漏项）：
- §6.4「在响应中标注『未按指标定义口径直接计算』」在本批 = `ChatResponse.degradedReason="explicit_time_scope"` + 流式 meta 帧同名字段；**不改写 LLM 生成的正文**，前端渲染留 P5（本批只透传）。
- §6.3「routing 指标记录 `tier=A→B`」本批**不新增写入点**：`RoutingMetricsService` 是只读聚合（读已落库的 `session_message.routing_layer`），Tier 分布看板本来就排在 P5（§10 阶段表）。P2 的可观测性由 `degraded_reason` 字段 + Tier A 失败 warning 日志（含 cause 链）承担。若 P5 要统计 `tier=A→B`，从 `ChatResponse`/meta 帧的 `executionTier` 取数即可，不需要新增表。

---

## 1. 事实基线（已实测，2026-11-11）

| 事实 | 证据 |
|---|---|
| 12 条 KPI：8 条 ESCAPE_HATCH（metric id 2–9）、4 条 NONE（10–13）；全部 `datasource_id=1`（THBI Oracle） | dev `qa_metadata` @5433 实测 |
| `SWITCH_TIER_A = "KPI_METRIC_TIER_A_ENABLED"` 已存在但**无读取函数**、`config.py` 无 env 字段 | `kpi_metric_switches.py:22`、`config.py:237-243` |
| 开关读取器 fail-closed，savepoint 包住查询 | `kpi_metric_switches.py:29-56` |
| `ReconcileResult.isTierAEligible = pass and not degraded` | `kpi_metric_reconcile.py:155` |
| 治理/技术快照构建器可直接复用 | `kpi_metric_spec.py:26` `governanceSpecOf`、`:35` `techniqueOf` |
| 诊断接口已有 `_tierOf`（无 metric/NONE ⇒ C；eligible ⇒ A；其余 B） | `api/v1/kpi_diagnose.py:103` |
| L1 现状：`_buildL1Response` → `_executeCalculationLogic` 按 **FeatureDefinition** 执行，不是 `metric.formula` | `chat_service.py:1530`、`:1647` |
| preamble 返回 `PreambleResult \| None`；普通/流式入口各一处调用 | `chat_service.py:450`、`chat_stream_entry.py:183` |
| L4 **不注入** KPI 契约块 ⇒ Tier B 必须跳过 L4 | `chat_l4.py:40-80` |
| adapter 已有只读事务 + `asyncio.wait_for(queryTimeoutSeconds=30)` + `fetchmany(queryRowLimit)`；`queryRowLimit` 默认 0=不限 | `business_db_pool.py:517-557`、`config.py:90-91` |
| `execute_read_only(sql)` 无 `maxRows` 参数 ⇒ 本批需加关键字参数（向后兼容） | `business_db_pool.py:441/518/592` |
| `METRIC_RESULT` 证据调度器已存在 | `evidence_record_service.py:265` `scheduleMetricResultEvidence` |
| `ChatResponse` 已有 `kpi_code`/`kpi_name`/`confidence`/`match_method`，**无** tier/reason | `domain/schemas.py:2007+` |
| `time_scope_resolver.parseTimeScope` 只解析季度/半年/年，**不解析**「近 N 天/月」 | `time_scope_resolver.py` |
| 测试库：`qa-pg-a1:5434/qa_metadata_test`；集成测试用真实 PG + `httpx.AsyncClient`；`DataSource.password_encrypted` 用 `encryptApiKey` 播种真实密码 | `_pg_support.py:40-69`、`test_research_resume_model_id.py:53` |
| integration 分片按文件名排序 `index mod 4` ⇒ 新增测试文件会轮转分片，属已知假 net-new 面 | `.github/workflows/ci.yml:353-364` |

---

## 2. 不变量

1. **开关关 = 旧行为**：Tier A 开关关时，preamble 走原 `_buildL1Response`（含 FD 执行）→ 返回 `L1`；`executionTier="LEGACY"`、`degradedReason="tier_a_disabled"` 只作观测，不改变任何分支。
2. **Tier B 绝不进 L4**：L4 不注入契约块；降级到 Tier B 时必须 `return` 一个 `L1B` 哨兵，由入口继续主 NL2SQL 流水线。
3. **路由判定唯一来源**：运行时 `planKpiExecution` 与诊断接口 `_tierOf` 必须同源（同一 `reconcileMetric` 输入）。用 parity 测试钉住。
4. **不做任何嗅探**：只看 `execution_source` 列，不看 formula 文本像不像 SQL。
5. **不可变**：`KpiExecutionPlan` / `KpiExecutionResult` 为 frozen dataclass；`ChatResponse` 用 `model_copy(update=...)` 派生，不改原对象。
6. **零静默**：任何不执行/降级都有 `reason` 常量 + warning 日志（含 cause 链）。
7. **数据源只认 `kpi.datasource_id`**：为空 ⇒ Tier C，不猜、不回退 `dto.datasourceId`。
8. **行数/超时/只读**由 adapter 统一兜底；执行器只传 `maxRows`，不自己拼 LIMIT（方言风险）。

---

## 3. 路由决策表（`planKpiExecution` 唯一口径）

按顺序短路，第一个命中即返回：

| # | 条件 | tier | reason |
|---|---|---|---|
| 1 | `switch_on=False` | `LEGACY` | `tier_a_disabled` |
| 2 | `kpi_found=False` | `LEGACY` | `kpi_not_found`（保持现有「匹配到但目录不存在 → 继续 L4/主链」行为） |
| 3 | `metric_present=False` | `C` | `metric_missing` |
| 4 | `execution_source` 归一后为空 | `C` | `execution_source_missing` |
| 5 | `execution_source == "NONE"` | `C` | `execution_source_none` |
| 6 | `execution_source == "COMPILED"` | `B` | `compiled_not_supported` |
| 7 | `execution_source` 非上述任何值 | `C` | `execution_source_unknown` |
| 8 | `execution_source == "ESCAPE_HATCH"` 且 `has_datasource=False` | `C` | `datasource_missing` |
| 9 | `execution_source == "ESCAPE_HATCH"` 且 `has_datasource=True` 且 `has_explicit_time_scope=True` | `B` | `explicit_time_scope` |
| 10 | `execution_source == "ESCAPE_HATCH"` 且 `has_datasource=True` 且 `formula_present=False` | `B` | `formula_empty` |
| 11 | `execution_source == "ESCAPE_HATCH"` 且 `has_datasource=True` 且 `tier_a_eligible=False` | `B` | `reconcile_not_eligible` |
| 12 | 其余 | `A` | `None` |

> `formula_empty` 放在 eligible 之前：formula 为空时对账层 1/3 不判，eligibility 可能为真，但执行无意义 ⇒ B。顺序即代码顺序，表驱动用例逐条钉住。
> 未知来源标 C（设计 §6.1）且写 warning；诊断接口 `_tierOf` 对未知来源仍判 B——**本批不改诊断**，parity 测试只覆盖 ESCAPE_HATCH/COMPILED/NONE 三类已知值。

---

# D1（第 1 天）— 开关 + 路由决策（纯函数）

**目标**：先把「哪条路」钉死，不碰 DB、不碰 chat。

### Task 1.1 开关：env 字段 + fail-closed 读取

**Files**
- Modify: `backend/app/config.py`（`:243` 之后）
- Modify: `backend/app/services/kpi_metric_switches.py`（`:109` 之后）
- Test: `backend/app/tests/unit/test_kpi_metric_tier_a_switch.py`（新建）

**Step 1 — RED**

```python
"""KPI_METRIC_TIER_A_ENABLED 双闸：env 杀闸优先，system_config 行缺失/非法 ⇒ 关。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import kpi_metric_switches as switches


class _FakeSettings:
    def __init__(self, enabled: bool) -> None:
        self.kpiMetricTierAEnabled = enabled


class _FakeSavepoint:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        if self._value is None:
            return None
        return SimpleNamespace(value=self._value)


class _FakeSession:
    """只实现 readKpiMetricSwitch 需要的 begin_nested / execute。"""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def begin_nested(self) -> _FakeSavepoint:
        return _FakeSavepoint()

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self._value)


@pytest.mark.asyncio()
async def test_env_kill_switch_wins(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(False))
    assert await switches.isKpiMetricTierAEnabled(_FakeSession("true")) is False


@pytest.mark.asyncio()
async def test_row_missing_is_false(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(True))
    assert await switches.isKpiMetricTierAEnabled(_FakeSession(None)) is False


@pytest.mark.asyncio()
async def test_row_true_is_true(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(True))
    assert await switches.isKpiMetricTierAEnabled(_FakeSession("true")) is True


@pytest.mark.asyncio()
async def test_none_session_is_false(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(True))
    assert await switches.isKpiMetricTierAEnabled(None) is False
```

**Step 2 — 跑 RED**

```bash
cd /private/tmp/qa-kpi-p2/backend
pytest app/tests/unit/test_kpi_metric_tier_a_switch.py -q
```
预期：`AttributeError: module ... has no attribute 'isKpiMetricTierAEnabled'`。

**Step 3 — GREEN**

`config.py`：

```python
    # P2 Tier A 确定性执行器灰度开关（env 只作一键止血；system_config 行才是旋钮）
    kpiMetricTierAEnabled: bool = Field(
        default=True, alias="KPI_METRIC_TIER_A_ENABLED"
    )
```

`kpi_metric_switches.py`：

```python
async def isKpiMetricTierAEnabled(session: AsyncSession | None) -> bool:
    """Tier A 执行器双闸（fail-closed）。

    1. env ``KPI_METRIC_TIER_A_ENABLED=false`` ⇒ 一律回旧行为（进程级）；
    2. system_config 行 ``KPI_METRIC_TIER_A_ENABLED`` 为真才执行；
       **行缺失 / 非法 / 读取异常 ⇒ 关**。

    ⇒ 本批合入后行为不变：开 Tier A 是一次显式的数据操作（设计 §12）。
    """
    if session is None:
        return False
    if not getSettings().kpiMetricTierAEnabled:
        logger.debug("Tier A env 紧急杀闸关闭，回落旧 L1 行为")
        return False
    return await readKpiMetricSwitch(session, SWITCH_TIER_A, False)
```

**Step 4 — GREEN 验证**：4 条全绿；并跑 `pytest app/tests/integration/test_kpi_switch_unification.py -q` 确认未破坏既有开关语义。

**Step 5 — 变异取证（必做）**：把 `return default` 改成 `return True`（`readKpiMetricSwitch` 行缺失分支），重跑本文件 ⇒ `test_row_missing_is_false` 必须 RED；还原。把输出贴进 `Harness/changes/2026-11-11-kpi-tier-a-executor/evidence/mutation-d1-switch.txt`。

**Step 6 — 提交**：`feat: KPI Tier A 双闸开关（fail-closed）`

### Task 1.2 路由纯函数

**Files**
- Create: `backend/app/services/kpi_execution_service.py`（本日只放常量 + `KpiExecutionPlan` + `planKpiExecution`）
- Test: `backend/app/tests/unit/test_kpi_execution_plan.py`（新建）

**Step 1 — RED**（表驱动，覆盖 §3 全部 12 行 + 优先级）

```python
import pytest

from app.services.kpi_execution_service import (
    KpiExecutionPlan,
    planKpiExecution,
)

_ALL_OK = dict(
    switch_on=True,
    kpi_found=True,
    metric_present=True,
    execution_source="ESCAPE_HATCH",
    tier_a_eligible=True,
    has_datasource=True,
    formula_present=True,
    has_explicit_time_scope=False,
)


def _plan(**overrides) -> KpiExecutionPlan:
    return planKpiExecution(**{**_ALL_OK, **overrides})


@pytest.mark.parametrize(
    ("overrides", "tier", "reason"),
    [
        ({"switch_on": False}, "LEGACY", "tier_a_disabled"),
        ({"switch_on": False, "execution_source": "NONE"}, "LEGACY", "tier_a_disabled"),
        ({"kpi_found": False}, "LEGACY", "kpi_not_found"),
        ({"metric_present": False}, "C", "metric_missing"),
        ({"execution_source": None}, "C", "execution_source_missing"),
        ({"execution_source": "  "}, "C", "execution_source_missing"),
        ({"execution_source": "NONE"}, "C", "execution_source_none"),
        ({"execution_source": "none"}, "C", "execution_source_none"),
        ({"execution_source": "COMPILED"}, "B", "compiled_not_supported"),
        ({"execution_source": "WEIRD"}, "C", "execution_source_unknown"),
        ({"has_datasource": False}, "C", "datasource_missing"),
        ({"has_explicit_time_scope": True}, "B", "explicit_time_scope"),
        ({"tier_a_eligible": False}, "B", "reconcile_not_eligible"),
        ({"formula_present": False}, "B", "formula_empty"),
        ({}, "A", None),
    ],
)
def test_plan_matrix(overrides, tier, reason) -> None:
    plan = _plan(**overrides)
    assert plan.tier == tier
    assert plan.reason == reason


def test_plan_is_frozen() -> None:
    plan = _plan()
    with pytest.raises(Exception):
        plan.tier = "B"  # type: ignore[misc]
```

**Step 2 — RED 验证**：`ModuleNotFoundError` / `ImportError`。

**Step 3 — GREEN**

```python
"""KPI Tier A 执行路由与确定性执行（设计 §6）。

路由只读 ontology_metric.execution_source，不嗅探 formula 文本；开关关闭时
整个模块不参与决策（调用方回旧 L1 行为）。
"""

from __future__ import annotations

from dataclasses import dataclass

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"
TIER_LEGACY = "LEGACY"

SOURCE_ESCAPE_HATCH = "ESCAPE_HATCH"
SOURCE_COMPILED = "COMPILED"
SOURCE_NONE = "NONE"

REASON_TIER_A_DISABLED = "tier_a_disabled"
REASON_KPI_NOT_FOUND = "kpi_not_found"
REASON_METRIC_MISSING = "metric_missing"
REASON_EXECUTION_SOURCE_MISSING = "execution_source_missing"
REASON_EXECUTION_SOURCE_NONE = "execution_source_none"
REASON_EXECUTION_SOURCE_UNKNOWN = "execution_source_unknown"
REASON_COMPILED_NOT_SUPPORTED = "compiled_not_supported"
REASON_DATASOURCE_MISSING = "datasource_missing"
REASON_EXPLICIT_TIME_SCOPE = "explicit_time_scope"
REASON_RECONCILE_NOT_ELIGIBLE = "reconcile_not_eligible"
REASON_FORMULA_EMPTY = "formula_empty"


@dataclass(frozen=True)
class KpiExecutionPlan:
    tier: str
    reason: str | None = None


def planKpiExecution(
    *,
    switch_on: bool,
    kpi_found: bool,
    metric_present: bool,
    execution_source: str | None,
    tier_a_eligible: bool,
    has_datasource: bool,
    formula_present: bool,
    has_explicit_time_scope: bool,
) -> KpiExecutionPlan:
    """按 §6.1/§6.2/§6.4 顺序短路（见 plan.md §3 决策表）。"""
    if not switch_on:
        return KpiExecutionPlan(TIER_LEGACY, REASON_TIER_A_DISABLED)
    if not kpi_found:
        return KpiExecutionPlan(TIER_LEGACY, REASON_KPI_NOT_FOUND)
    if not metric_present:
        return KpiExecutionPlan(TIER_C, REASON_METRIC_MISSING)
    source = (execution_source or "").strip().upper()
    if not source:
        return KpiExecutionPlan(TIER_C, REASON_EXECUTION_SOURCE_MISSING)
    if source == SOURCE_NONE:
        return KpiExecutionPlan(TIER_C, REASON_EXECUTION_SOURCE_NONE)
    if source == SOURCE_COMPILED:
        return KpiExecutionPlan(TIER_B, REASON_COMPILED_NOT_SUPPORTED)
    if source != SOURCE_ESCAPE_HATCH:
        return KpiExecutionPlan(TIER_C, REASON_EXECUTION_SOURCE_UNKNOWN)
    if not has_datasource:
        return KpiExecutionPlan(TIER_C, REASON_DATASOURCE_MISSING)
    if has_explicit_time_scope:
        return KpiExecutionPlan(TIER_B, REASON_EXPLICIT_TIME_SCOPE)
    if not formula_present:
        return KpiExecutionPlan(TIER_B, REASON_FORMULA_EMPTY)
    if not tier_a_eligible:
        return KpiExecutionPlan(TIER_B, REASON_RECONCILE_NOT_ELIGIBLE)
    return KpiExecutionPlan(TIER_A)
```

> 顺序说明：`formula_present` 放在 `tier_a_eligible` 之前（formula 空时对账层 1/3 不判、eligible 可能为真），
> 与 §3 决策表 #10/#11 一致。

**Step 4 — GREEN 验证**：`pytest app/tests/unit/test_kpi_execution_plan.py -q` 全绿。

**Step 5 — 变异取证**：把 `SOURCE_COMPILED` 分支的 `TIER_B` 改成 `TIER_A` ⇒ 对应用例必须 RED；把 `not has_datasource` 分支删掉 ⇒ 对应用例必须 RED。输出存 `evidence/mutation-d1-plan.txt`。

**Step 6 — 提交**：`feat: Tier A 执行路由决策纯函数`

---

# D2（第 2 天）— 只读执行器 + 行数上限 + 归一化

### Task 2.1 adapter 增加 `maxRows`（向后兼容）

**Files**
- Modify: `backend/app/infrastructure/business_db_pool.py`
- Test: `backend/app/tests/unit/test_business_db_pool_row_limit.py`（新建）

**Step 1 — RED**

```python
from app.infrastructure.business_db_pool import _effectiveRowLimit


def test_explicit_max_rows_wins(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.infrastructure.business_db_pool.getSettings",
        lambda: type("S", (), {"queryRowLimit": 0})(),
    )
    assert _effectiveRowLimit(1000) == 1000


def test_configured_limit_is_upper_bound(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.infrastructure.business_db_pool.getSettings",
        lambda: type("S", (), {"queryRowLimit": 50})(),
    )
    assert _effectiveRowLimit(1000) == 50
    assert _effectiveRowLimit(10) == 10


def test_no_max_rows_keeps_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.infrastructure.business_db_pool.getSettings",
        lambda: type("S", (), {"queryRowLimit": 0})(),
    )
    assert _effectiveRowLimit(None) == 0
```

**Step 2 — RED 验证**：`ImportError`。

**Step 3 — GREEN**

```python
def _effectiveRowLimit(maxRows: int | None) -> int:
    """显式 maxRows 与 QUERY_ROW_LIMIT 取更小正数；都为 0/None ⇒ 不限行。"""
    configured = getSettings().queryRowLimit
    if maxRows is not None and maxRows > 0:
        if configured <= 0:
            return maxRows
        return min(maxRows, configured)
    return configured
```

Protocol 与两个实现签名改为：

```python
async def execute_read_only(
    self, sql: str, *, maxRows: int | None = None
) -> list[dict[str, Any]]:
```

`_SqlaAdapter` 内 `limit = _effectiveRowLimit(maxRows)`；`_OracleAdapter` 同样。其余逻辑不动。

**Step 4 — GREEN 验证**：本文件全绿 + `pytest app/tests/unit/test_feature_compute_service.py app/tests/unit/test_data_quality_evaluator_time_window.py -q`（现有 adapter 调用方回归）。

**Step 5 — 真实 DB 验证（集成）**

新建 `backend/app/tests/integration/test_kpi_tier_a_execution_pg.py`：

```python
import pytest
from sqlalchemy.engine import make_url

from app.domain.enums import DataSourceType
from app.domain.models import DataSource
from app.infrastructure.business_db_pool import dispose_adapter, get_adapter
from app.infrastructure.security.crypto import encryptApiKey
from app.tests._pg_support import resolveTestDatabaseUrl

pytestmark = pytest.mark.asyncio


async def _seedSelfDatasource(dbSession) -> DataSource:
    """把测试元数据库自身当作业务库：真实 asyncpg + 真实只读事务，不建任何表。"""
    url = make_url(resolveTestDatabaseUrl())
    ds = DataSource(
        name="tier-a-self-pg",
        type=DataSourceType.POSTGRESQL,
        host=url.host,
        port=url.port or 5432,
        database_name=url.database,
        username=url.username,
        password_encrypted=encryptApiKey(url.password or ""),
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def test_max_rows_truncates_real_query(dbSession) -> None:
    ds = await _seedSelfDatasource(dbSession)
    adapter = get_adapter(ds.id, ds)
    try:
        rows = await adapter.execute_read_only(
            "SELECT g AS n FROM generate_series(1, 50) AS g", maxRows=10
        )
    finally:
        dispose_adapter(ds.id)
    assert len(rows) == 10
```

（`generate_series` 是 PG 内置，不依赖任何业务表；`dispose_adapter` 防模块级缓存串味。）

**Step 6 — 提交**：`feat: 业务库 adapter 支持单次 maxRows 上限`

### Task 2.2 执行器本体

**Files**
- Modify: `backend/app/services/kpi_execution_service.py`
- Test: `backend/app/tests/unit/test_kpi_execution_service.py`（新建）

**Step 1 — RED**（关键用例）

```python
from app.domain.exceptions import SqlSafetyError
from app.services.kpi_execution_service import (
    KpiExecutionResult,
    KpiExecutionService,
    firstPresentValue,
)


class _StubAdapter:
    def __init__(self, rows=None, exc=None) -> None:
        self.rows = rows or []
        self.exc = exc
        self.calls = []

    async def execute_read_only(self, sql, *, maxRows=None):
        self.calls.append((sql, maxRows))
        if self.exc is not None:
            raise self.exc
        return self.rows


async def test_happy_path_returns_value_and_metadata() -> None:
    adapter = _StubAdapter(rows=[{"rate": None}, {"rate": 0.93}])
    svc = KpiExecutionService(adapterProvider=lambda _id, _ds: adapter)
    result = await svc.execute(
        formula="SELECT 0.93 AS rate", datasourceId=7, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=0.9,
    )
    assert result.ok is True
    assert result.value == 0.93
    assert result.rowCount == 2
    assert result.reason is None
    assert adapter.calls == [("SELECT 0.93 AS rate", 1001)]  # rowLimit + 1 用于精确判截断


async def test_empty_rows_degrades() -> None:
    svc = KpiExecutionService(adapterProvider=lambda _i, _d: _StubAdapter(rows=[]))
    result = await svc.execute(
        formula="SELECT 1", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False
    assert result.reason == "empty_result"
    assert result.rows == ()


async def test_all_none_value_degrades() -> None:
    svc = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(rows=[{"v": None}])
    )
    result = await svc.execute(
        formula="SELECT NULL AS v", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == "empty_result"


async def test_adapter_timeout_degrades() -> None:
    svc = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(exc=TimeoutError("t"))
    )
    result = await svc.execute(
        formula="SELECT 1", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == "execution_timeout"


async def test_adapter_error_degrades_without_raising() -> None:
    svc = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(exc=RuntimeError("boom"))
    )
    result = await svc.execute(
        formula="SELECT 1", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == "execution_error"


async def test_non_readonly_formula_is_rejected_before_adapter() -> None:
    adapter = _StubAdapter(rows=[{"x": 1}])
    svc = KpiExecutionService(adapterProvider=lambda _i, _d: adapter)
    result = await svc.execute(
        formula="DELETE FROM t", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == "formula_not_read_only"
    assert adapter.calls == []  # 绝不允许落到 adapter


def test_first_present_value_skips_none() -> None:
    assert firstPresentValue([{"a": None, "b": 2}]) == 2
    assert firstPresentValue([]) is None
```

另加：
- `test_row_limit_marks_truncated`：adapter 返回 **1001** 行 ⇒ `truncated is True` 且 `len(result.rows) == 1000`；返回 **1000** 行 ⇒ `truncated is False` 且 1000 行全保留（这条钉住「多取 1 行」的精确语义，防止有人把 `>` 改成 `>=`）。
- `test_evidence_scheduler_failure_does_not_break`：注入必然抛错的 scheduler，仍 `ok=True`。

**Step 2 — RED 验证**：`ImportError` / `AttributeError` 符合预期。

**Step 3 — GREEN**（追加到 `kpi_execution_service.py`）

```python
import logging
import time
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource
from app.domain.exceptions import SqlSafetyError
from app.infrastructure.business_db_pool import (
    BusinessDbAdapter,
    _assert_read_only,
    get_adapter,
)
from app.services.evidence_record_service import scheduleMetricResultEvidence

logger = logging.getLogger(__name__)

KPI_RESULT_ROW_LIMIT = 1000

REASON_FORMULA_NOT_READ_ONLY = "formula_not_read_only"
REASON_EXECUTION_TIMEOUT = "execution_timeout"
REASON_EXECUTION_ERROR = "execution_error"
REASON_EMPTY_RESULT = "empty_result"

AdapterProvider = Callable[[int, Any], BusinessDbAdapter]


@dataclass(frozen=True)
class KpiExecutionResult:
    ok: bool
    rows: tuple[dict[str, Any], ...] = ()
    value: Any | None = None
    rowCount: int = 0
    truncated: bool = False
    elapsedMs: int = 0
    reason: str | None = None


def firstPresentValue(rows: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> Any | None:
    """首行第一个非 None 值（KPI 结果读值的唯一口径）。"""
    if not rows:
        return None
    return next((v for v in rows[0].values() if v is not None), None)


class KpiExecutionService:
    """执行 metric.formula：只读校验 → adapter（超时/行数由 adapter 兜底）→ 归一化。"""

    def __init__(
        self,
        *,
        adapterProvider: AdapterProvider | None = None,
        evidenceScheduler: Callable[..., Any] = scheduleMetricResultEvidence,
    ) -> None:
        self._adapterProvider = adapterProvider or get_adapter
        self._evidenceScheduler = evidenceScheduler

    async def execute(
        self,
        *,
        formula: str,
        datasourceId: int,
        datasource: DataSource,
        metricCode: str,
        metricName: str,
        confidence: float,
        rowLimit: int = KPI_RESULT_ROW_LIMIT,
    ) -> KpiExecutionResult:
        try:
            _assert_read_only(formula)  # 执行器侧第一道；adapter 内部还会再验
        except SqlSafetyError:
            logger.warning(
                "TierA formula 未通过只读校验 kpi=%s datasource_id=%s",
                metricCode, datasourceId, exc_info=True,
            )
            return KpiExecutionResult(ok=False, reason=REASON_FORMULA_NOT_READ_ONLY)

        started = time.monotonic()
        adapter = self._adapterProvider(datasourceId, datasource)
        try:
            # 多取 1 行用于**精确**判断截断（len==rowLimit 不代表被截断）。
            # 若运维把 QUERY_ROW_LIMIT 设成与 rowLimit 相同，adapter 会先截到该值，
            # 此时截断检测退化为 len>=rowLimit（保守：宁可标 truncated，不瞒报）。
            raw = await adapter.execute_read_only(formula, maxRows=rowLimit + 1)
        except TimeoutError:
            logger.warning(
                "TierA 执行超时 kpi=%s datasource_id=%s elapsed_ms=%d",
                metricCode, datasourceId, _elapsedMs(started), exc_info=True,
            )
            return KpiExecutionResult(
                ok=False, elapsedMs=_elapsedMs(started), reason=REASON_EXECUTION_TIMEOUT
            )
        except Exception:
            logger.warning(
                "TierA 执行失败 kpi=%s datasource_id=%s elapsed_ms=%d",
                metricCode, datasourceId, _elapsedMs(started), exc_info=True,
            )
            return KpiExecutionResult(
                ok=False, elapsedMs=_elapsedMs(started), reason=REASON_EXECUTION_ERROR
            )

        fetched = tuple(dict(r) for r in (raw or []))
        truncated = len(fetched) > rowLimit
        rows = fetched[:rowLimit] if truncated else fetched
        elapsed = _elapsedMs(started)
        value = firstPresentValue(rows)
        if not rows or value is None:
            logger.warning(
                "TierA 空结果 kpi=%s datasource_id=%s rows=%d elapsed_ms=%d",
                metricCode, datasourceId, len(rows), elapsed,
            )
            return KpiExecutionResult(
                ok=False, rows=rows, rowCount=len(rows), elapsedMs=elapsed,
                reason=REASON_EMPTY_RESULT,
            )

        result = KpiExecutionResult(
            ok=True, rows=rows, value=value, rowCount=len(rows), truncated=truncated,
            elapsedMs=elapsed,
        )
        logger.info(
            "TierA 执行成功 kpi=%s datasource_id=%s rows=%d elapsed_ms=%d truncated=%s",
            metricCode, datasourceId, len(rows), elapsed, truncated,
        )
        self._scheduleEvidence(
            metricCode=metricCode, metricName=metricName, confidence=confidence,
            rows=rows, datasourceId=datasourceId,
        )
        return result

    def _scheduleEvidence(self, **kwargs: Any) -> None:
        """METRIC_RESULT best-effort：调度失败只 warning，不影响主链路。"""
        try:
            self._evidenceScheduler(**kwargs)
        except Exception:
            logger.warning("TierA METRIC_RESULT 证据调度失败（忽略）", exc_info=True)


def _elapsedMs(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
```

`scheduleMetricResultEvidence` 的签名是 keyword-only（`metricCode`/`metricName`/`confidence`/`rows`/`datasourceId`），与上面调用一致。

**Step 4 — GREEN 验证**：本文件全绿；`pytest app/tests/unit/test_kpi_execution_plan.py -q` 仍绿。

**Step 5 — 变异取证**：
- 删掉 `_assert_read_only(formula)` ⇒ `test_non_readonly_formula_is_rejected_before_adapter` 必须 RED（并且 adapter.calls 非空）；
- 把 `except TimeoutError` 并进 `except Exception` 前删掉 ⇒ timeout 用例 reason 变成 `execution_error`，必须 RED。
输出存 `evidence/mutation-d2-executor.txt`。

**Step 6 — 提交**：`feat: Tier A 只读执行器（Guard/超时/行数/归一化/证据）`

---

# D3（第 3 天）— 时间范围检测 + 降级日志/计量收口

### Task 3.1 显式时间限定检测

**Files**
- Modify: `backend/app/services/kpi_execution_service.py`
- Test: `backend/app/tests/unit/test_kpi_time_scope_detection.py`（新建）

**Step 1 — RED**

```python
import pytest

from app.services.kpi_execution_service import hasExplicitTimeScope


@pytest.mark.parametrize(
    "question",
    [
        "近6个月供应商准时交付率",
        "最近 30 天的逾期率",
        "过去三个月的交付率",
        "2025年准时交付率",
        "2025-03 的交付率",
        "本季度交付率",
        "第二季度交付率",
        "Q2 交付率",
        "上半年交付率",
        "2025年上半年逾期率",
    ],
)
def test_detects_explicit_time(question: str) -> None:
    assert hasExplicitTimeScope(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "供应商准时交付率是多少",
        "KPI_SUPPLIER_OTD 这个指标",
        "逾期率为什么下降",
        "交付率",
    ],
)
def test_plain_question_has_no_time_scope(question: str) -> None:
    assert hasExplicitTimeScope(question) is False
```

**Step 2 — RED 验证**：`ImportError`。

**Step 3 — GREEN**

```python
import re

# 一期只做“有/无”判定，不做区间解析（P4 参数化才解析）。
# 不调用 time_scope_resolver.parseTimeScope：它只认季度/半年，且这些形态已被下面的
# 正则覆盖；复用会把「检测口径」与「注入口径」耦合，P4 改解析器时会连带改变路由。
_EXPLICIT_TIME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(近|最近|过去|前)\s*[0-9一二三四五六七八九十两]+\s*(天|日|周|星期|个月|月|年|季度)"),
    re.compile(r"(近|最近|过去)\s*半\s*年"),
    re.compile(r"[0-9]{4}\s*年"),
    re.compile(r"[0-9]{4}\s*[-/]\s*[0-9]{1,2}"),
    re.compile(r"[0-9]{1,2}\s*月份?"),
    re.compile(r"(本|上|下|这|当前)(周|星期|个月|月|季度|年|年度)"),
    re.compile(r"(今年|去年|明年|前年|本年|上年)"),
    re.compile(r"第?\s*[一二三四1-4]\s*季度"),
    re.compile(r"(上|下)半年"),
    re.compile(r"\bQ[1-4]\b", re.IGNORECASE),
)


def hasExplicitTimeScope(question: str) -> bool:
    """问句是否含显式时间限定（设计 §6.4：有 ⇒ 不路由 Tier A）。

    宁可多判（误判 ⇒ Tier B，多一次 LLM）不可漏判（漏判 ⇒ 时段错的数字看起来合理）。
    匹配到任一形态即视为显式；无数字的「近半年/这半年」也计入（滚动窗口语义有分歧，
    交给 LLM 按用户原话处理）。
    """
    return any(p.search(question) for p in _EXPLICIT_TIME_PATTERNS)
```

**Step 4 — GREEN 验证**：全绿。

**Step 5 — 变异取证**：删掉 `re.compile(r"[0-9]{4}\s*年")` 这一条 ⇒ `"2025年准时交付率"` 用例必须 RED（其余形态仍命中，证明该条不是死代码）。输出存 `evidence/mutation-d3-time.txt`。

**Step 6 — 提交**：`feat: Tier A 显式时间限定检测（一期只判有无）`

### Task 3.2 执行器-证据-日志的接线测试

**Files**
- Test: `backend/app/tests/unit/test_kpi_execution_service.py`（追加）

补齐：
- `test_success_schedules_metric_result_evidence`：注入记录型 scheduler，断言收到 `metricCode/metricName/confidence/rows/datasourceId`。
- `test_scheduler_failure_does_not_change_result`：scheduler 抛 `RuntimeError`，`result.ok` 仍 True。
- `test_failure_does_not_schedule_evidence`：空结果/异常时 scheduler 未被调用。
- `test_log_line_contains_kpi_and_datasource`：`caplog` 断言成功行含 `kpi=`、`datasource_id=`、`rows=`、`elapsed_ms=`；失败行含 `TierA 执行失败`。

**提交**：`test: Tier A 计量/日志接线`

---

# D4（第 4 天）— 接入 preamble（路由落地，不接 ChatResponse 字段）

**目标**：把 D1–D3 的判定接进 `_runL1AndL4Preamble`，证明四条分支互不串线。

### Task 4.1 `L1B` 哨兵 + 计划钩子

**Files**
- Modify: `backend/app/services/chat_constants.py`
- Modify: `backend/app/services/chat_stream_output.py`
- Modify: `backend/app/services/chat_service.py`（新增 `_planKpiTier`/`_buildTierCResponse` 的最小实现）
- Test: `backend/app/tests/unit/test_chat_tier_a_preamble.py`（新建）

**Step 1 — RED**

```python
"""Tier A/B/C/LEGACY 四分支的 preamble 选择；Tier B 绝不允许落 L4。"""

import pytest

from app.domain.schemas import ChatRequest
from app.services.chat_stream_output import L1L4PreambleMixin
from app.services.kpi_semantic_match_service import KpiMatchResult, KpiMatchRoute


class _Kpi:
    kpi_code = "KPI_SUPPLIER_OTD"
    kpi_name = "供应商准时交付率"
    semantic_keywords = ["准时交付"]
    metric_id = 5
    datasource_id = 1
    business_definition = "按承诺日期交付"
    unit = "%"


class _Matcher:
    """v2 路径桩：matchRoute 恒返回唯一命中。"""

    async def matchRoute(self, _q: str) -> KpiMatchRoute:
        return KpiMatchRoute(
            kind="hit",
            matches=(
                KpiMatchResult(
                    code="KPI_SUPPLIER_OTD",
                    kpi_name="供应商准时交付率",
                    confidence=0.95,
                    method="containment",
                ),
            ),
        )


class _Preamble(L1L4PreambleMixin):
    def __init__(self, *, tierPlan, executeResult=None, executeReason=None) -> None:
        self._kpiMatcher = _Matcher()
        self.plan = tierPlan
        self.executeResult = executeResult
        self.executeReason = executeReason
        self.l4Called = False
        self.tierACalled = False
        self.legacyCalled = False

    async def _containmentEnabled(self, session):  # noqa: ANN001
        return True

    async def _ensureKpiCacheFresh(self, session):  # noqa: ANN001
        return None

    async def _resolveKpiCatalog(self, code, session):  # noqa: ANN001
        return _Kpi()

    async def _planKpiTier(self, question, kpi, session):  # noqa: ANN001
        return self.plan

    async def _executeTierA(self, match, kpi, session):  # noqa: ANN001
        self.tierACalled = True
        return self.executeResult, self.executeReason

    async def _buildL1Response(self, match, session):  # noqa: ANN001
        self.legacyCalled = True
        return type("R", (), {
            "answer": "legacy", "chartType": None, "chartOption": None,
            "visual_rationale": None, "kpi_name": "n",
        })()

    async def _handleNl2SqlAgent(self, session, dto, user):  # noqa: ANN001
        self.l4Called = True
        return None


def _dto() -> ChatRequest:
    return ChatRequest(question="供应商准时交付率", sessionId="s", datasourceId=1)


@pytest.mark.asyncio()
async def test_tier_a_builds_l1_response_and_skips_l4() -> None:
    response = type("R", (), {
        "answer": "tier-a", "chartType": None, "chartOption": None,
        "visual_rationale": None, "kpi_name": "供应商准时交付率",
    })()
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "A", "reason": None})(),
        executeResult=response,
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None and preamble.routing_layer == "L1"
    assert mixin.tierACalled is True and mixin.l4Called is False
    assert mixin.legacyCalled is False


@pytest.mark.asyncio()
async def test_tier_b_returns_sentinel_and_skips_l4() -> None:
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "B", "reason": "explicit_time_scope"})()
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None
    assert preamble.routing_layer == "L1B"
    assert preamble.degraded_reason == "explicit_time_scope"
    assert mixin.l4Called is False  # L4 不注入契约块，必须跳过
    assert mixin.legacyCalled is False


@pytest.mark.asyncio()
async def test_tier_c_returns_l1_without_execution() -> None:
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "C", "reason": "execution_source_none"})()
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None and preamble.routing_layer == "L1"
    assert preamble.execution_tier == "C"
    assert mixin.tierACalled is False and mixin.l4Called is False


@pytest.mark.asyncio()
async def test_legacy_calls_build_l1_response() -> None:
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "LEGACY", "reason": "tier_a_disabled"})()
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None and preamble.routing_layer == "L1"
    assert preamble.execution_tier == "LEGACY"
    assert mixin.legacyCalled is True


@pytest.mark.asyncio()
async def test_tier_a_execution_failure_degrades_to_b() -> None:
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "A", "reason": None})(),
        executeResult=None,
        executeReason="execution_error",
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None and preamble.routing_layer == "L1B"
    assert preamble.degraded_reason == "execution_error"
    assert mixin.l4Called is False
```

**Step 2 — RED 验证**：`ImportError`/`AttributeError`（`routing_layer "L1B"` 未定义即失败）。

**Step 3 — GREEN**

`chat_constants.py`：

```python
# Tier B 降级哨兵：preamble 返回它 ≠ 终结，入口必须继续主 NL2SQL 流水线。
# 仅进程内使用，不落 session_message.routing_layer（仍按 L2 落库）。
ROUTING_LAYER_KPI_TIER_B = "L1B"
```

`chat_stream_output.py`：

```python
from app.services.kpi_execution_service import (
    REASON_EXECUTION_ERROR,
    REASON_RECONCILE_NOT_ELIGIBLE,
    TIER_A, TIER_B, TIER_C, TIER_LEGACY,
    planKpiExecution,
)
from app.services.kpi_metric_switches import isKpiMetricTierAEnabled

class PreambleResult(BaseModel):
    routing_layer: Literal["L1", "L4", "L1C", "L1B"]
    # 现有字段（answer / full_response / kpi_code / kpi_name / confidence /
    # datasource_mismatch / warning 等）全部保持不变，只改上面的 Literal 并追加下面三行。
    execution_tier: str | None = None
    degraded_reason: str | None = None
    kpi_match_method: str | None = None


class L1L4PreambleMixin:
    async def _tierAEnabled(self, session: AsyncSession) -> bool:
        """单点（测试可覆写）。session=None（纯单元）⇒ 关。"""
        return await isKpiMetricTierAEnabled(session)
```

`_runL1AndL4Preamble` 的 KPI 分支改为（**整段替换**，含现有 mismatch 检查）：

```python
        match = route.matches[0] if route is not None and route.kind == "hit" else None
        if match is not None:
            # 先取 KPI 行：四条分支都要用它（Tier 判定 / 文案 / datasource 比较）。
            kpi = await self._resolveKpiCatalog(match.code, session)
            plan = await self._planKpiTier(dto.question, kpi, session)

            if plan.tier == TIER_B:
                # L4 不注入契约块：Tier B 必须交给主 NL2SQL 流水线。
                return self._tierBPreamble(match, kpi, plan.reason)

            if plan.tier == TIER_A:
                l1, degradedReason = await self._executeTierA(match, kpi, session)
                if l1 is None:
                    return self._tierBPreamble(
                        match, kpi, degradedReason or REASON_EXECUTION_ERROR
                    )
            elif plan.tier == TIER_C:
                l1 = self._buildTierCResponse(match, kpi, plan.reason)
            else:  # TIER_LEGACY：开关关 / 目录缺失 ⇒ 现有 FD 执行路径
                l1 = await self._buildL1Response(match, session)

            if l1 is not None:
                # P0 合并项 6：仅在 KPI 实际绑定了数据源时比较（沿用原判断）。
                mismatch = (
                    kpi is not None
                    and kpi.datasource_id is not None
                    and kpi.datasource_id != dto.datasourceId
                )
                warning = (
                    MSG_KPI_DATASOURCE_MISMATCH.format(
                        datasource_id=kpi.datasource_id,
                        user_datasource_id=dto.datasourceId,
                    )
                    if mismatch else None
                )
                return PreambleResult(
                    routing_layer=ROUTING_LAYER_L1,
                    answer=l1.answer,
                    chart_type=l1.chartType,
                    chart_option=l1.chartOption,
                    visual_rationale=l1.visual_rationale,
                    kpi_code=match.code,
                    kpi_name=l1.kpi_name,
                    confidence=match.confidence,
                    kpi_match_method=match.method,
                    execution_tier=plan.tier,
                    degraded_reason=plan.reason,
                    datasource_mismatch=mismatch,
                    mismatch_warning=warning,
                    full_response=l1,
                )
```

> 只替换现有 `if match is not None:` 块（`chat_stream_output.py:300-325`）。其后的
> `# 2. L4 Agent Loop`（`:327-342`）**原样保留**——`l1 is None`（含 Tier C 构造失败 /
> LEGACY 且目录缺失）仍按现状落到 L4，行为不变。
> `_tierBPreamble` / `_planKpiTier` / `_buildTierCResponse` 全部定义在
> `L1L4PreambleMixin` 上（`ChatService` 继承该 mixin），不要写进 `ChatService` 类。
> `_executeTierA` 的**真实实现**在 D5 才落地；D4 只要保证「开关 fail-closed ⇒ 永不触达 Tier A 分支」，
> 因此现有集成测试（测试库无开关行）不会走到它。D4 的单测桩覆写 `_executeTierA` 即可。

`_tierBPreamble` 辅助方法（同文件，避免两处重复构造）：

```python
    @staticmethod
    def _tierBPreamble(
        match: KpiMatchResult, kpi: Any | None, reason: str | None
    ) -> PreambleResult:
        """Tier B 哨兵：preamble 不终结，入口拆包后继续主 NL2SQL 流水线。"""
        return PreambleResult(
            routing_layer=ROUTING_LAYER_KPI_TIER_B,
            answer="",
            kpi_code=match.code,
            kpi_name=(kpi.kpi_name if kpi is not None else match.code),
            confidence=match.confidence,
            kpi_match_method=match.method,
            execution_tier=TIER_B,
            degraded_reason=reason,
        )
```

`chat_service.py` 新增（本日最小实现，D5 再完善）：

```python
    async def _planKpiTier(
        self, question: str, kpi: KpiCatalog | None, session: AsyncSession
    ) -> KpiExecutionPlan:
        """运行时 Tier 判定：与诊断接口共用 reconcileMetric，避免两套口径。"""
        switchOn = await self._tierAEnabled(session)
        hasExplicitTime = hasExplicitTimeScope(question)

        # 开关关：不碰 kpi 属性（单测里的 kpi 桩可能只有 code/name/keywords）。
        if not switchOn:
            return planKpiExecution(
                switch_on=False, kpi_found=kpi is not None, metric_present=False,
                execution_source=None, tier_a_eligible=False, has_datasource=False,
                formula_present=False, has_explicit_time_scope=hasExplicitTime,
            )
        if kpi is None:
            return planKpiExecution(
                switch_on=True, kpi_found=False, metric_present=False,
                execution_source=None, tier_a_eligible=False, has_datasource=False,
                formula_present=False, has_explicit_time_scope=hasExplicitTime,
            )

        metric = (
            await session.get(OntologyMetric, kpi.metric_id)
            if kpi.metric_id is not None else None
        )
        # metric 缺失时不做对账（techniqueOf(None) 虽可构造，但此时对账结论无意义）。
        reconcile = (
            reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))
            if metric is not None else None
        )
        return planKpiExecution(
            switch_on=True,
            kpi_found=True,
            metric_present=metric is not None,
            execution_source=(metric.execution_source if metric is not None else None),
            tier_a_eligible=bool(reconcile is not None and reconcile.isTierAEligible),
            has_datasource=kpi.datasource_id is not None,
            formula_present=bool(metric is not None and (metric.formula or "").strip()),
            has_explicit_time_scope=hasExplicitTime,
        )
```

`chat_service.py` 顶部 import（第 56 行**扩展**，`DataSource` 已在）：

```python
from app.domain.models import (
    DataSource, KpiCatalog, LlmConfig, OntologyClass, OntologyMetric,
    SessionMessage, SessionQueryState,
)
from app.services.kpi_execution_service import (
    REASON_DATASOURCE_MISSING,
    REASON_METRIC_MISSING,
    TIER_A, TIER_B, TIER_C, TIER_LEGACY,
    KpiExecutionPlan,
    hasExplicitTimeScope,
    planKpiExecution,
)
from app.services.kpi_metric_reconcile import reconcileMetric
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf
```

`_buildTierCResponse`（同步方法，不查库）：

```python
    def _buildTierCResponse(
        self, match, kpi, reason: str | None
    ) -> ChatResponse:
        """Tier C：指标不可确定性计算 ⇒ 只给口径说明（0 LLM）。

        reason 透传计划原因（execution_source_none / metric_missing /
        datasource_missing / execution_source_unknown），不再硬编码单一常量。
        """
        name = kpi.kpi_name or match.code
        answer = (
            f"指标「{name}」：{kpi.business_definition or '暂无口径说明'}"
            "（暂不可确定性计算）"  # 设计 §6.1 原文措辞，集成测试按此断言
        )
        return self._wrapChatResponse(
            match, name, None, answer, unit=kpi.unit,
            executionTier=TIER_C, degradedReason=reason,
        )
```

> `_wrapChatResponse` 的 `executionTier/degradedReason` 参数与 `ChatResponse` 字段**在 D4 Task 4.1 一并落地**（代码见 D5 Task 5.1），避免 D4 写完 D5 再改签名。
> `TIER_LEGACY` / `TIER_B` 在本日仅用于路由与哨兵，尚无响应字段消费；D5 落地字段后由 `_attachKpiHint` / `execution_tier` 消费。

**Step 4 — GREEN 验证**：本文件全绿；`pytest app/tests/unit/test_chat_preamble_kpi_route.py -q` 仍全绿（开关默认关 ⇒ LEGACY 路径，桩 `_buildL1Response` 仍被调用）。

**Step 5 — 变异取证**：把 Tier B 分支改成 `return None`（即落 L4）⇒ `test_tier_b_returns_sentinel_and_skips_l4` 与 `test_tier_a_execution_failure_degrades_to_b` 必须 RED。输出存 `evidence/mutation-d4-routing.txt`。

**Step 6 — 提交**：`feat: preamble 接入 Tier A/B/C/LEGACY 路由（Tier B 跳过 L4）`

---

# D5（第 5 天）— ChatResponse 字段 + 两个入口 + 流式 meta

### Task 5.1 schema 与响应构造

**Files**
- Modify: `backend/app/domain/schemas.py`（`ChatResponse`，`modelName` 之后）
- Modify: `backend/app/services/chat_service.py`（`_wrapChatResponse`、`_executeTierA`、`processMessage`）

**Step 1 — RED**（追加到 `backend/app/tests/integration/test_kpi_tier_a_chat_api.py`，见 Task 5.3；先写 schema 单测）

`backend/app/tests/unit/test_chat_response_tier_fields.py`：

```python
from app.domain.schemas import ChatResponse


def test_tier_fields_default_none() -> None:
    r = ChatResponse(answer="a", intent="l1_match")
    assert r.execution_tier is None and r.degraded_reason is None


def test_tier_fields_serialize_camel_case() -> None:
    r = ChatResponse(
        answer="a", intent="l1_match",
        execution_tier="B", degraded_reason="explicit_time_scope",
    )
    payload = r.model_dump(by_alias=True, mode="json")
    assert payload["executionTier"] == "B"
    assert payload["degradedReason"] == "explicit_time_scope"
```

**Step 2 — RED 验证**：`ValidationError` / `TypeError`（未知字段）。

**Step 3 — GREEN**

```python
    # P2 Tier A：执行分层与降级原因（观测字段；不改变前端既有渲染分支）。
    execution_tier: str | None = Field(
        default=None, description="KPI 执行分层：A | B | C | LEGACY"
    )
    degraded_reason: str | None = Field(
        default=None, description="未走 Tier A 的原因码（设计 §6）"
    )
```

`_wrapChatResponse` 增加关键字参数并透传：

```python
    def _wrapChatResponse(
        self, match, kpi_name, data, answer, unit=None,
        *, executionTier: str | None = None, degradedReason: str | None = None,
    ) -> ChatResponse:
        ...
        return ChatResponse(
            ..., execution_tier=executionTier, degraded_reason=degradedReason,
        )
```

`_executeTierA`（在 `ChatService` 中）：

```python
    async def _executeTierA(self, match, kpi, session) -> tuple[ChatResponse | None, str | None]:
        """Tier A：执行 metric.formula；失败返回 (None, reason) 交 preamble 降级。"""
        if kpi is None or kpi.metric_id is None:
            return None, REASON_METRIC_MISSING
        metric = await session.get(OntologyMetric, kpi.metric_id)
        if metric is None:
            return None, REASON_METRIC_MISSING
        datasource = (
            await session.get(DataSource, kpi.datasource_id)
            if kpi.datasource_id is not None else None
        )
        if datasource is None:
            return None, REASON_DATASOURCE_MISSING

        service = self._kpiExecutionService  # 构造器注入，测试可替换
        result = await service.execute(
            formula=metric.formula,
            datasourceId=kpi.datasource_id,
            datasource=datasource,
            metricCode=match.code,
            metricName=kpi.kpi_name,
            confidence=match.confidence,
        )
        if not result.ok:
            logger.warning(
                "Tier A 降级 kpi=%s reason=%s", match.code, result.reason
            )
            return None, result.reason

        data = [dict(r) for r in result.rows]
        text = self._buildAnswerText(kpi, data, kpi.kpi_name or match.code)
        response = self._wrapChatResponse(
            match, kpi.kpi_name or match.code, data, text, unit=kpi.unit,
            executionTier="A",
        )
        if result.truncated:
            logger.warning("Tier A 结果截断 kpi=%s row_count=%d", match.code, result.rowCount)
        return response, None
```

`ChatService.__init__` 增加构造参数 + 实例字段：

```python
    def __init__(
        self,
        *,
        ...,
        kpiMatcher: KpiSemanticMatchService | None = None,
        kpiExecutionService: Any | None = None,   # P2 Tier A 执行器（测试可注入桩）
    ) -> None:
        ...
        # P2 Tier A：复用同一个 adapterProvider（生产 = get_adapter；测试可注入假 adapter）。
        from app.services.kpi_execution_service import KpiExecutionService
        self._kpiExecutionService = kpiExecutionService or KpiExecutionService(
            adapterProvider=self._adapterProvider
        )
```

**Step 4 — GREEN 验证**：两个单测全绿。

### Task 5.2 非流式 + 流式入口接线

**Files**
- Modify: `backend/app/services/chat_service.py`（`processMessage`，`:450` 起）
- Modify: `backend/app/services/chat_stream_entry.py`（`:183` 起）

**Step 1 — RED**（单元）

`backend/app/tests/unit/test_chat_tier_b_hint.py`：

```python
"""Tier B 哨兵拆包 + SSE meta 字段形状（纯函数，无 IO）。"""

from app.services.chat_stream_output import (
    PreambleResult,
    kpiHintFields,
    splitTierBPreamble,
)


def _tierB() -> PreambleResult:
    return PreambleResult(
        routing_layer="L1B",
        answer="",
        kpi_code="KPI_X",
        kpi_name="指标X",
        confidence=0.9,
        kpi_match_method="containment",
        execution_tier="B",
        degraded_reason="explicit_time_scope",
    )


def test_split_tier_b_sentinel_returns_hint() -> None:
    preamble, hint = splitTierBPreamble(_tierB())
    assert preamble is None
    assert hint is not None and hint.routing_layer == "L1B"


def test_split_non_sentinel_returns_preamble() -> None:
    l1 = PreambleResult(routing_layer="L1", answer="a", execution_tier="A")
    preamble, hint = splitTierBPreamble(l1)
    assert preamble is l1 and hint is None


def test_split_none() -> None:
    assert splitTierBPreamble(None) == (None, None)


def test_hint_fields_wire_shape() -> None:
    assert kpiHintFields(_tierB()) == {
        "kpiCode": "KPI_X",
        "kpiName": "指标X",
        "confidence": 0.9,
        "matchMethod": "containment",
        "executionTier": "B",
        "degradedReason": "explicit_time_scope",
    }


def test_hint_fields_none_is_empty() -> None:
    assert kpiHintFields(None) == {}
```

为可测性，把入口判断抽成纯函数（放在 `chat_stream_output.py`）：

```python
def splitTierBPreamble(
    preamble: PreambleResult | None,
) -> tuple[PreambleResult | None, PreambleResult | None]:
    """Tier B 哨兵拆包：返回 (终结型 preamble, Tier B 提示)。"""
    if preamble is not None and preamble.routing_layer == ROUTING_LAYER_KPI_TIER_B:
        return None, preamble
    return preamble, None


def kpiHintFields(hint: PreambleResult | None) -> dict[str, Any]:
    if hint is None:
        return {}
    return {
        "kpiCode": hint.kpi_code,
        "kpiName": hint.kpi_name,
        "confidence": hint.confidence,
        "matchMethod": hint.kpi_match_method,
        "executionTier": hint.execution_tier,
        "degradedReason": hint.degraded_reason,
    }
```

**Step 2 — RED 验证**：`ImportError`。

**Step 3 — GREEN**

`chat_service.py`：

```python
        # :450 原 `preamble = await self._runL1AndL4Preamble(...)` 改为下面两行
        preamble, kpiHint = splitTierBPreamble(
            await self._runL1AndL4Preamble(dto, session, user)
        )
        if preamble is not None:
            ... 现有分支不变（L1/L1C/L4 早退）...
        # ...中间各分类早退分支（CHITCHAT/DEFINE/MAP/METRIC/...）原样保留...
        # :504 原 `response = await self._handleGenericQuery(...)` 之后插入 3 行
        response = await self._handleGenericQuery(session, dto, result, state, user=user)
        if response is not None:
            response = self._attachKpiHint(response, kpiHint)
        # ...suggested_agent model_copy / return response 原样保留...
        return response
```

```python
    @staticmethod
    def _attachKpiHint(response: ChatResponse, hint: PreambleResult | None) -> ChatResponse:
        """Tier B 提示挂到主链路响应（不可变：model_copy 派生）。"""
        if hint is None or response is None:
            return response
        return response.model_copy(update={
            "kpi_code": hint.kpi_code,
            "kpi_name": hint.kpi_name,
            "confidence": hint.confidence,
            "match_method": hint.kpi_match_method,
            "execution_tier": hint.execution_tier,
            "degraded_reason": hint.degraded_reason,
        })
```

> `_handleAgentRun` 等分类前置分支的早退不挂 hint（它们不是 Tier B 的目标路径）；在代码注释里写明。Tier B 的目标路径是 `_handleGenericQuery`。

`chat_stream_entry.py`：

```python
        preamble, kpiHint = splitTierBPreamble(
            await self._runL1AndL4Preamble(dto, session, user)
        )
        if preamble is not None:
            meta_frame = {
                "intent": metaIntentOf(preamble.routing_layer),
                "routingLayer": preamble.routing_layer,
                "datasourceMismatch": preamble.datasource_mismatch,
            }
            # Tier A/C/LEGACY 的观测字段（Tier B 走下面的 kpiHint 分支）。
            if preamble.execution_tier is not None:
                meta_frame["executionTier"] = preamble.execution_tier
                meta_frame["degradedReason"] = preamble.degraded_reason
                meta_frame["matchMethod"] = preamble.kpi_match_method
            ... 其余帧不变（agentRun / agent_trace / sql / chart / token / done）...
            return

        # 非 preamble 路径有两个 META 帧（首次分类 :228、重分类 :248），
        # 两处都要带同一份 Tier B 字段：前端逐帧合并，漏一处会在重分类后被冲掉。
        hintFields = kpiHintFields(kpiHint)
        firstPass = await self.classifyAndRecall(session, dto.question, needRecall=False)
        result = firstPass.intentResult
        yield StreamEvent(EVENT_META, {"intent": result.intent.value, **hintFields})
        ...
                yield StreamEvent(EVENT_META, {"intent": result.intent.value, **hintFields})
```

**Step 4 — GREEN 验证**：`pytest app/tests/unit/test_chat_tier_b_hint.py -q`；再跑 `pytest app/tests/integration/test_chat_l1_routing.py app/tests/integration/test_chat_stream_l1_l4.py -q`（真实 PG，确认既有 L1/流式路径未回归）。

**Step 5 — 变异取证**：把 `_attachKpiHint` 改成 `return response`（不挂 hint）⇒ 5.3 的 Tier B 集成断言必须 RED。输出存 `evidence/mutation-d5-hint.txt`。

**Step 6 — 提交**：`feat: ChatResponse 暴露 executionTier/degradedReason 并接线两入口`

### Task 5.3 全链路集成测试（真实 PG，完整 API）

**Files**
- Create: `backend/app/tests/integration/test_kpi_tier_a_chat_api.py`

**断言的种子数据**（全部 `dbSession` 播种，用例间自动 TRUNCATE）：

> 模块级 autouse fixture：每个用例前后 `await reset_pool()`（`business_db_pool` 导出），
> 避免 `_adapters` 按 datasourceId 缓存跨用例串味（TRUNCATE 不会清进程内缓存）。

1. `DataSource`：指向测试库自身（同 Task 2.1 `_seedSelfDatasource`），真实 asyncpg；
2. `OntologyMetric`（对账 pass 的最小形状，已按 `reconcileMetric` 源码逐层核对）：
   ```python
   metric = OntologyMetric(
       metric_name="TierA测试指标技术形态",
       formula="SELECT COUNT(*) AS cnt FROM kpi_catalog",
       metric_type="MEASURE",                 # 非 RATIO ⇒ 不查分母、不查 aggregation_order
       numerator_measure={"agg": "COUNT", "expr": "cnt"},
       grain_keys=["kpi_code"],
       execution_source="ESCAPE_HATCH",
   )
   ```
   层 1/3：结构化列 `{"cnt"}` ⊆ formula 标识符 `{cnt, kpi_catalog, ...}` ⇒ 不降级；
   层 2：分子 `{"cnt"}` == 技术 `{"cnt"}`，粒度 `{"kpi_code"}` == `{"kpi_code"}` ⇒ PASS。
3. `KpiCatalog`：`kpi_code="KPI_TIER_A_OK"`、`kpi_name="TierA测试指标"`、`business_definition="按测试元数据表计数"`、`semantic_keywords=["TierA测试指标"]`、`numerator_columns=["cnt"]`、`grain_keys=["kpi_code"]`、`unit="个"`、`metric_id`、`datasource_id`、`status="PUBLISHED"`；
4. 开关行（**两行都要**）：
   ```python
   from app.models.system_config import SystemConfig
   from app.services.kpi_metric_switches import SWITCH_MATCH_CONTAINMENT, SWITCH_TIER_A

   dbSession.add_all([
       SystemConfig(key=SWITCH_MATCH_CONTAINMENT, value="true"),
       SystemConfig(key=SWITCH_TIER_A, value="true"),
   ])
   ```
   v2 包含式匹配默认关；不开它就走 v1 关键字匹配，「TierA测试指标」未必命中。
5. **KPI 匹配缓存必须重建**（全局单例不随 TRUNCATE 清空，与 `test_kpi_diagnose_api.py:_warmCache` 同法）：
   ```python
   from app.services.kpi_match_cache import get_kpi_match_cache

   cache = get_kpi_match_cache()
   cache.onKpiChanged()
   await cache.warmUp(dbSession)
   ```
   `test_time_scoped_question_degrades_to_tier_b` 同样需要这一步——它走的是同一条匹配路径。

**用例**

- `test_plain_question_executes_tier_a_zero_llm`：
  `POST /api/v1/chat`（非流式，`X-User-Id` 默认头）问「TierA测试指标是多少」；
  断言 `200`；`intent == "l1_match"`；`tokensUsed == 0`；`cost == 0.0`；`executionTier == "A"`；`degradedReason is None`；`data[0]` 的数值与直接 `SELECT COUNT(*) FROM kpi_catalog` 一致（同一连接内先查基线，容差 0.01）。
- `test_none_source_returns_tier_c_text`：另一条 KPI 绑 `execution_source="NONE"`；断言 `executionTier == "C"`、`degradedReason` 含 `execution_source_none`、正文含「暂不可确定性计算」、`tokensUsed == 0`。
- `test_switch_row_missing_keeps_legacy_behavior`：**只播种 `KPI_MATCH_CONTAINMENT_ENABLED`**（保证能匹配到），**不播种 `KPI_METRIC_TIER_A_ENABLED`**；非流式接口断言 `executionTier == "LEGACY"`、`degradedReason == "tier_a_disabled"`，且 `data is None`（旧 FD 路径找不到锚点 ⇒ 不执行、不报错）。
- `test_time_scoped_question_degrades_to_tier_b`：用流式接口（`POST /api/v1/chat/stream`）问「近6个月TierA测试指标」；解析 SSE，断言首个 `meta` 帧含 `executionTier == "B"`、`degradedReason == "explicit_time_scope"`，且**不含** `routingLayer`（preamble 未终结 ⇒ 哨兵被拆包）。「没进 L4」由 D4 单测 `test_tier_b_returns_sentinel_and_skips_l4` 钉住，不在 SSE 上做间接推断。
- `test_formula_execution_error_degrades_to_tier_b`：单独种一条 KPI（关键字「TierA报错指标」），metric.formula = `"SELECT cnt FROM kpi_catalog"`——`cnt` 是真实表里不存在的列，**对账仍 pass**（标识符集合含 `cnt`），执行时才报错。流式接口断言 meta `degradedReason == "execution_error"`。
  > 不能用 `SELECT * FROM no_such_table`：结构化列 `cnt` 不在 formula 标识符里 ⇒ 层 1/3 mismatch ⇒ 先被 `reconcile_not_eligible` 拦下，测不到执行异常。
- `test_non_readonly_formula_never_reaches_adapter`：单独种一条 KPI（关键字「TierA写指标」），metric.formula = `"DELETE FROM kpi_catalog WHERE cnt > 0"`。先用 `reconcileMetric` 断言 `isTierAEligible is True`（证明该 formula 能过对账，确实会走到只读校验），再打非流式接口：`executionTier == "B"`、`degradedReason == "formula_not_read_only"`；用例末尾断言种子 KPI 行仍在（`SELECT COUNT(*) FROM kpi_catalog WHERE kpi_code=...` 与原值一致），证明写语句从未执行。

**Step 1 — RED**：文件新建后跑，全部失败（无实现）。
**Step 2 — GREEN 验证**：

```bash
cd /private/tmp/qa-kpi-p2/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/integration/test_kpi_tier_a_chat_api.py -q
```

**Step 3 — 提交**：`test: Tier A 全链路集成（真实 PG + 完整 API）`

---

# D6（第 6 天）— M2 验收 + 真实数据对比 + 变异取证

### Task 6.1 真实数据对比（dev，手动，不进 CI）

**Files**
- Create: `backend/scripts/kpi_tier_a_probe.py`

**要求**：
- 默认 `--dry-run`：只打印 12 条 KPI 的路由计划（tier/reason），不发任何查询；
- `--execute --kpi-code KPI_XXX`：直接调用 `KpiExecutionService.execute`（**不经过 chat、不读开关行**），打印 `value/rowCount/elapsedMs/truncated`；
- 必须显式传 `--database-url`（元数据库）与 `--datasource-id`，默认值留空；禁止连生产；脚本启动时校验目标库名（复用 `_pg_support.assertTestDatabase` 的白名单逻辑，仅允许 `qa_metadata` / `qa_metadata_test*`）。

**执行步骤（手动，记录到 `evidence/tier-a-realdata.txt`）**：

1. 对 8 条 ESCAPE_HATCH KPI 逐条跑 `--dry-run`，确认全部 `tier=A`；
2. 对 4 条 NONE KPI 跑 `--dry-run`，确认全部 `tier=C/execution_source_none`；
3. 对 8 条各跑 `--execute`，记录数值；
4. 用同一 formula 在只读客户端（psql / sqlplus）手工执行，逐条对比，差值 ≤ 0.01 记为 PASS；
5. 结果表写进 `evidence/tier-a-realdata.txt`（8 行：kpi_code / tier_a_value / manual_value / diff / pass）。

> 若 dev Oracle THBI 不可达，**如实记录不可达**，不得伪造数值；此时 M2 数值验收降级为「集成测试（真实 PG 业务数据源）通过的 1 条 + 其余 7 条待环境就绪」并在 summary 里标明未完成项。

### Task 6.2 变异取证汇总

对以下五个变异逐个执行（改 → 跑定向 → 必须 RED → 还原），输出合并进 `evidence/mutation-summary.txt`：

| # | 变异 | 必须变红的用例 |
|---|---|---|
| 1 | `readKpiMetricSwitch` 行缺失返回 `True` | `test_row_missing_is_false` |
| 2 | `planKpiExecution` 把 COMPILED 判成 A | `test_plan_matrix[COMPILED]` |
| 3 | 执行器删掉 `_assert_read_only` | `test_non_readonly_formula_is_rejected_before_adapter` |
| 4 | Tier B 分支返回 `None`（落 L4） | `test_tier_b_returns_sentinel_and_skips_l4` |
| 5 | `_attachKpiHint` 不挂字段 | Tier B 集成断言 |

### Task 6.3 覆盖率

```bash
pytest app/tests/unit/test_kpi_execution_plan.py \
       app/tests/unit/test_kpi_execution_service.py \
       app/tests/unit/test_kpi_time_scope_detection.py \
       app/tests/unit/test_kpi_metric_tier_a_switch.py \
       --cov=app.services.kpi_execution_service \
       --cov=app.services.kpi_metric_switches \
       --cov-report=term-missing
```
要求新模块 ≥ 80%；缺口补用例，不写 `# pragma: no cover` 掩盖。

**提交**：`test: Tier A 变异取证与覆盖率`

---

# D7（第 7 天）— 回归、评审、文档

### Task 7.1 定向回归（不跑后端全量 unit——已知 27min/28 红基线）

**7.1a 新用例必须全绿**（这些文件在本分支才有，不能拿去 main 跑）：

```bash
cd /private/tmp/qa-kpi-p2/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest \
  app/tests/unit/test_kpi_execution_plan.py \
  app/tests/unit/test_kpi_execution_service.py \
  app/tests/unit/test_kpi_time_scope_detection.py \
  app/tests/unit/test_kpi_metric_tier_a_switch.py \
  app/tests/unit/test_chat_tier_a_preamble.py \
  app/tests/unit/test_chat_tier_b_hint.py \
  app/tests/unit/test_chat_response_tier_fields.py \
  app/tests/integration/test_kpi_tier_a_execution_pg.py \
  app/tests/integration/test_kpi_tier_a_chat_api.py -q
```

**7.1b 既有邻近套件做失败集合对差**（同一条命令在**本分支**与 **`origin/main` 的 main worktree** 各跑一次；
基线命令里**不得**包含上面那 9 个新文件——main 上没有它们，pytest 会直接报 file not found）：

```bash
pytest \
  app/tests/unit/test_chat_preamble_kpi_route.py \
  app/tests/integration/test_chat_l1_routing.py \
  app/tests/integration/test_chat_l1_calculation_resolution.py \
  app/tests/integration/test_chat_stream_l1_l4.py \
  app/tests/integration/test_kpi_switch_unification.py \
  app/tests/integration/test_kpi_diagnose_api.py -q
```

判据：**失败集合对差净新增 0**（分支侧失败集合 ⊆ main 侧失败集合）。`cancelled` / UNUSABLE_JOBS 不算通过，须整轮重跑。
若 main 侧某条本来就红，在 summary 里列明并证明同一条在分支侧同样红（不是新引入）。

### Task 7.2 双重评审

- `code-reviewer` agent：重点看 D5 的两入口接线、不可变性、错误路径；
- `security-reviewer` agent：重点看只读链路是否可被 formula 绕过、数据源越权、日志是否泄漏连接串/密码。
- 两条评审发现按 CRITICAL/HIGH/MEDIUM 处理；CRITICAL 必修。

### Task 7.3 文档

**Files**
- Create: `Harness/changes/2026-11-11-kpi-tier-a-executor/summary.md`
- Modify: `Harness/wiki/nl2sql-engine.md`（或 Architecture 对应页，按现有文件选择）——记录「L1 命中后的 Tier A/B/C/LEGACY 分流图」与 `executionTier` 字段。

`summary.md` 必须包含：背景 / 路由决策表 / 变更清单（逐日 commit）/ 验收证据（M2 六条逐条打勾或如实标注未完成）/ 未做事项（P3/P4、生产开关行、部署）/ 回滚说明（关开关即回旧行为）。

### Task 7.4 收尾

- `git status` 必须干净（`backend/.venv` 等被 gitignore 的不算）；
- push 分支 + 开 PR（`HTTPS_PROXY=http://127.0.0.1:12450`），PR 描述引用本 plan 与 evidence；
- **不合并、不部署、不创建任何生产 `system_config` 行**（等用户另行批准）。

---

## 4. 文件清单

| 文件 | 动作 |
|---|---|
| `backend/app/config.py` | +`kpiMetricTierAEnabled` env 字段 |
| `backend/app/services/kpi_metric_switches.py` | +`isKpiMetricTierAEnabled`（fail-closed） |
| `backend/app/services/kpi_execution_service.py` | 新建：路由纯函数 + 执行器 + 时间限定检测 |
| `backend/app/infrastructure/business_db_pool.py` | `execute_read_only(..., *, maxRows=None)` + `_effectiveRowLimit` |
| `backend/app/services/chat_constants.py` | +`ROUTING_LAYER_KPI_TIER_B = "L1B"` |
| `backend/app/services/chat_stream_output.py` | `PreambleResult` 新字段、`_tierAEnabled`、四分支路由、`splitTierBPreamble`/`kpiHintFields` |
| `backend/app/services/chat_service.py` | `_planKpiTier`、`_executeTierA`、`_buildTierCResponse`、`_attachKpiHint`、`_wrapChatResponse` 参数、`processMessage` 接线 |
| `backend/app/services/chat_stream_entry.py` | Tier B 哨兵拆包 + meta 帧字段 |
| `backend/app/domain/schemas.py` | `ChatResponse.execution_tier` / `degraded_reason` |
| `backend/app/tests/unit/*` | 7 个新测试文件（开关/路由/执行器/时间/预检/hint/字段） |
| `backend/app/tests/integration/test_kpi_tier_a_execution_pg.py` | 新建（adapter 真实 PG 行数上限） |
| `backend/app/tests/integration/test_kpi_tier_a_chat_api.py` | 新建（全链路 API） |
| `backend/scripts/kpi_tier_a_probe.py` | 新建（dry-run 默认，手动验收用） |
| `Harness/changes/2026-11-11-kpi-tier-a-executor/{plan.md,summary.md,evidence/}` | 计划、总结、取证 |
| `Harness/wiki/nl2sql-engine.md` | Tier 分流说明 |
| **不新增 Alembic 迁移** | 零 DDL |

---

## 5. 风险与回滚

| 风险 | 评估 | 处置 |
|---|---|---|
| Tier B 未跳过 L4 ⇒ 契约块缺失，回答脱离口径 | 高 | D4 单测硬断言 `l4Called is False`；变异 #4 取证 |
| 开关读成「默认开」造成行为突变 | 高 | `isKpiMetricTierAEnabled` fail-closed + 变异 #1；生产行保持不存在 |
| 执行结果与定义口径不一致（可执行 SQL 本身错） | 中 | 对账 `isTierAEligible` 门禁；mismatch ⇒ Tier B，不执行 |
| 显式时间问题被确定性地答错时段 | 高 | D3 检测宁可多判；变异/表驱动覆盖 10 条正例 4 条反例 |
| 大结果拖垮内存 | 中 | `maxRows=1000` + adapter `fetchmany` + `truncated` 标记 |
| `maxRows` 改坏现有调用方 | 中 | 关键字参数默认 None；D2 跑 feature/DQ 回归 |
| 新增集成测试文件导致 CI 分片假 net-new | 中 | D7 只按「失败集合对差」判定；PR 描述里注明分片轮转 |
| 手动真实数据不可达 | 中 | 如实留档未完成，不用假数值充数 |
| 回滚 | — | 关 `KPI_METRIC_TIER_A_ENABLED`（env 或删/改行）即回旧行为；代码可整体 revert，无迁移、无数据变更 |
