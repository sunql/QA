"""KPI 指标技术形态治理：真实数据验证脚本（feat-kpi-metric-technical-form）。

目的
----
在**真实 PostgreSQL** 上验证本特性的关键不变量，每一步输出一行
``PASS`` / ``FAIL`` / ``SKIP``，并**无条件**打印结尾汇总（T16-14）。

模块划分（按职责拆，单文件均 < 800 行）
--------------------------------------
- ``kpi_metric_realdata_lib``：常量、DSN 解析、PASS/FAIL/SKIP 输出；
- ``kpi_metric_realdata_db``：只读 DB helper + 开关行的读写与还原；
- ``kpi_metric_realdata_s3``：S3 的判定 oracle（列差异 / 分母判定 / formula 覆盖）；
- ``kpi_metric_realdata_s4``：S4 发布闸门（开关 ON/OFF 的可还原写）；
- ``kpi_metric_realdata_inject``：S5 契约注入端到端（含审核模拟）；
- 本文件（入口）：S1–S9 各步骤与主流程。

覆盖的步骤
----------
1. **迁移与结构**：alembic 版本 = ``0137``；``ontology_metric`` 新增 9 列；
   ``ontology_metric_source`` 表 + ``UNIQUE(metric_id, class_id, role)`` +
   ``class_id`` 索引；``kpi_catalog`` 新增 3 列。
2. **回填预览与物理列校验**：PLAN 与**库内** ``kpi_catalog`` 行一一对应；
   tier A 的结构化列**必须**在真实 ``schema_cache`` 里存在。
3. **三层对账逐档**：**实测 12/12 全部 ``mismatch``** —— 这是 PLAN 刻意保留
   「治理列 = 业务口径、技术列 = 物理列」偏差的**预期结果**（T15-L），不是脚本缺陷。
   回填后治理列与物理列必然不同（``governanceGrain`` 是中文业务词、``grainKeys``
   是列名），故原先「tier A ⇒ pass / tier B/C ⇒ unknown、无一条 mismatch」的预测
   **不成立**。判据因此改为：**verdict 必须与 PLAN 已登记偏差集合一致**（S3.1/S3.3），
   且**列级 finding 集合必须与治理/技术列的实际差异一致**（S3.4，防 ``_checkRole``
   的列比对被删掉）。tier A 的 formula 仍须**覆盖全部结构化列**
   （否则「未降级」是恒真的，T16-17）。
4. **发布闸门**：开关 ON ⇒ 服务层抛 ``ConflictError`` 且带**缺项明细**
   （断言「是谁拒的 + 缺哪一项」，不是只断言状态码）；开关 OFF ⇒ 不拦。
   本轮临时写的开关**退出前还原**，并打印前后对照。
5. **契约块注入端到端**：含 KPI code 的问句（精确 alias 路径）⇒ 契约块非空 ⇒
   比率型文案写死 ``SUM(分子)/SUM(分母)`` 且**禁止** ``AVG(分子/分母)``；
   ⚠️ **反假绿**：断言 ``_buildPlanSystemPrompt`` 的返回值**真的包含**契约文本。
6. **精确 alias 匹配可用性**：含 KPI code 的问句 ⇒ ``matchAll`` 命中且
   ``confidence == 1.0``；同时**如实打印**自然语言问句的 Jaccard 最高分
   （已知限制 L1：5–6 词目录 vs 0.75 阈值 ⇒ Jaccard 路径实测不可达）。
7. **缓存动态性**：缓存条目数与库内 **PUBLISHED** KPI 条数一致；``hasCode`` 命中；
   ``findByAnyKeyword`` 能按已回填的关键词找到候选。
8. **``data_source`` 裸表名精确 join**：拆表名 ⇒ 命中索引的比例（如实打印），
   并断言拆分的**纯函数**行为。
9. **回填落库（仅 ``--apply-backfill``）**：跑两遍，验证幂等 + 12 条 KPI 全部挂靠。

写库边界
--------
脚本**默认只读**。唯一的写操作是 ``system_config`` 里的灰度开关行，退出前
**原样还原**。回填落库（写 ``ontology_metric`` / ``kpi_catalog.metric_id``）
要显式加 ``--apply-backfill``（**仅在业务方审核回填清单后**执行）。

用法
----
::

    # 正式库（prod 同源）—— 必须显式 opt-in（与是否真的写无关，守卫按库名判定）
    export KPI_METRIC_REALDATA_DATABASE_URL='postgresql+asyncpg://<user>:<pwd>@localhost:5433/qa_metadata'
    cd backend && ./.venv/bin/python -m scripts.kpi_metric_realdata --allow-prod-write

    # 隔离测试库（无需 opt-in）
    export KPI_METRIC_REALDATA_DATABASE_URL='postgresql+asyncpg://<user>:<pwd>@localhost:5434/qa_metadata_test_t16'
    cd backend && ./.venv/bin/python -m scripts.kpi_metric_realdata

    # 目标库也可以直接用命令行给（优先于环境变量）
    cd backend && ./.venv/bin/python -m scripts.kpi_metric_realdata \
      --database-url 'postgresql+asyncpg://<user>:<pwd>@localhost:5434/qa_metadata_test_t16'

    # 回填已过业务方审核、允许落库时（会写库 ⇒ 必须先按规范备份表）
    cd backend && ./.venv/bin/python -m scripts.kpi_metric_realdata \
      --database-url '…' --apply-backfill --allow-prod-write

⚠️ **本脚本不猜环境变量顺序**：``TEST_DATABASE_URL`` / ``DATABASE_URL`` **不参与**
目标解析 ⇒ 必须用 ``KPI_METRIC_REALDATA_DATABASE_URL`` 或 ``--database-url``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections import Counter
from pathlib import Path

# 允许从 scripts/ 直接运行：把 backend/ 放进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.kpi_metric_realdata_lib import (  # noqa: E402
    CONTRACT_QUESTION,
    EXPECTED_ALEMBIC_VERSION,
    MAX_STEPS,
    SKIPS,
    check,
    expectedVerdictFor,
    failStep,
    guardTarget,
    info,
    lastSkipReason,
    maskDsn,
    passStep,
    printSummary,
    resolveDatabaseUrl,
    skipStep,
)

# ---------------------------------------------------------------------------
# 配置（必须在 import app.* 之前解析：app.config 读 DATABASE_URL）
# ---------------------------------------------------------------------------


def _parseArgs(argv: list[str]) -> tuple[str | None, bool]:
    """极简参数解析：**必须在 import app.* 之前**跑。

    ``app.config`` 在 import 时就读 ``DATABASE_URL`` ⇒ 目标 DSN 必须在
    import 之前定下来，否则「连错库」连发现的机会都没有。
    """
    args = list(argv)
    databaseUrl: str | None = None
    for index, token in enumerate(args):
        if token == "--database-url" and index + 1 < len(args):
            databaseUrl = args[index + 1]
        elif token.startswith("--database-url="):
            databaseUrl = token.split("=", 1)[1]
    return databaseUrl, "--allow-prod-write" in args


_CLI_DATABASE_URL, _CLI_ALLOW_PROD_WRITE = _parseArgs(sys.argv[1:])

DATABASE_URL, DSN_SOURCE = resolveDatabaseUrl(_CLI_DATABASE_URL)
# 目标库守卫：**在接触数据库之前**拒绝非白名单库 / 未 opt-in 的正式库。
TARGET_DB = guardTarget(DATABASE_URL, allowProdWrite=_CLI_ALLOW_PROD_WRITE)
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ.setdefault("LOG_LEVEL", "WARNING")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.domain.models import OntologyMetric, SchemaCache  # noqa: E402
from app.infrastructure.database import getEngine, getSessionFactory  # noqa: E402
from app.services.kpi_datasource_suggestion_service import (  # noqa: E402
    buildTableIndex,
    splitLegacyTableNames,
)
from app.services.kpi_match_cache import get_kpi_match_cache  # noqa: E402
from app.services.kpi_metric_reconcile import (  # noqa: E402
    VERDICT_MISMATCH,
    reconcileMetric,
)
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf  # noqa: E402
from app.services.kpi_metric_switches import (  # noqa: E402
    SWITCH_CONTRACT_INJECTION,
    SWITCH_GATE,
    SWITCH_TIER_A,
)
from app.services.kpi_semantic_match_service import (  # noqa: E402
    getKpiSemanticMatchService,
)
from scripts.kpi_metric_backfill import (  # noqa: E402
    applyPlans,
    buildPlans,
    classColumnIndex,
    verifyPlan,
)
from scripts.kpi_metric_backfill_plan import KpiBackfillPlan  # noqa: E402
from scripts.kpi_metric_realdata_db import (  # noqa: E402
    countLinkedKpis,
    countMetrics,
    currentDatabaseName,
    fetchKpis,
    fetchLinkedMetricNames,
    hasIndexOn,
    readAlembicVersion,
    readSwitchValue,
    tableColumns,
    uniqueConstraintColumnSets,
)

# ⚠️ 必须在 ``os.environ["DATABASE_URL"]`` 设置**之后**导入：本模块连带导入
# ``app.services.*`` ⇒ 触发 ``app.config`` 读 DATABASE_URL。提前导入会连到
# 配置默认值（5432 的 OpenMetadata 库）并以认证失败告终。
from scripts.kpi_metric_realdata_inject import (  # noqa: E402
    step5_contractInjection,
)
from scripts.kpi_metric_realdata_s3 import (  # noqa: E402
    ReconcileOutcome,
    actualColumnCodes,
    anyColumnDiffers,
    expectedColumnCodes,
    formulaCoverage,
)
from scripts.kpi_metric_realdata_s4 import step4_publishGate  # noqa: E402

# PLAN 的构造保证（不是库内实测值；库内实测值另行打印）
EXPECTED_TIER_COUNTS = {"A": 4, "B": 5, "C": 3}

# 库内应有 12 条 KPI（prod / dev 同源实测；与 PLAN 一一对应）
EXPECTED_KPI_COUNT = 12

# 三个灰度开关（用于打印 preflight 存在性）
SWITCH_KEYS = (SWITCH_CONTRACT_INJECTION, SWITCH_GATE, SWITCH_TIER_A)

# S1 实测「迁移已应用」的结果。未应用时 S2–S8 的前置条件不成立 ⇒ 记 **SKIP**
# （不是 FAIL）：它们在新列缺失的库上根本没法执行，把「无法执行」判成「失败」
# 会让人误读成「特性有 7 个缺陷」。
_SCHEMA_READY = False

# 依赖 0136/0137 新结构的步骤（S1 之后才能跑）
_SCHEMA_DEPENDENT_STEPS = frozenset({"S2", "S3", "S4", "S5", "S6", "S7", "S8"})


def _tierOfCode(plans: dict[str, KpiBackfillPlan], code: str) -> str | None:
    """从 PLAN 索引里取 tier；不存在 ⇒ ``None``。"""
    plan = plans.get(code)
    return None if plan is None else plan.tier()


# ── preflight（观测，不是判定） ────────────────────────────────────────


async def _preflight(session: AsyncSession) -> None:
    """打印「连到了哪个库」与两类计数（验收要求：报清计数）。"""
    actualDb = await currentDatabaseName(session)
    if actualDb != TARGET_DB:
        # 第 4 闸：落库实测。不符 ⇒ **立刻中止**（继续跑就会写到没被守卫审视的库），
        # 但仍走 main 的 except/finally ⇒ 汇总照常打印。
        failStep(
            "PREFLIGHT current_database() = 守卫认定库名",
            f"实际 {actualDb!r}，守卫 {TARGET_DB!r}",
        )
        raise RuntimeError(
            f"落库实测库名不符：actual={actualDb!r} expected={TARGET_DB!r}"
        )
    passStep("PREFLIGHT current_database() = 守卫认定库名", actualDb)
    try:
        kpis = await fetchKpis(session)
        linked = await countLinkedKpis(session)
        metricCount = await countMetrics(session)
        info(
            f"计数：kpi_catalog={len(kpis)} 条（metric_id 非空 {linked} 条）；"
            f"ontology_metric={metricCount} 条"
        )
    except Exception as exc:  # noqa: BLE001 —— 结构未迁移时计数本就不可读
        # **观测失败不记 FAIL**：迁移未应用这件事由 S1.1 判定，preflight 只报事实。
        info(f"计数不可读（结构未迁移？）：{exc!r}")
        # PG 在语句报错后会把事务标记为 aborted ⇒ 必须回滚，否则后续查询全被拒。
        await session.rollback()
    info(f"alembic_version 实测 = {await readAlembicVersion(session)!r}")
    for key in SWITCH_KEYS:
        value = await readSwitchValue(session, key)
        state = "缺失（走代码默认值）" if value is None else f"存在 value={value!r}"
        info(f"system_config[{key}] {state}")


# ── S1 迁移与结构 ─────────────────────────────────────────────────────


async def step1_schema(session: AsyncSession) -> None:
    global _SCHEMA_READY
    version = await readAlembicVersion(session)
    _SCHEMA_READY = version == EXPECTED_ALEMBIC_VERSION
    check(
        "S1.1 迁移版本 = " + EXPECTED_ALEMBIC_VERSION,
        _SCHEMA_READY,
        f"实际 {version!r}",
    )

    metricColumns = await tableColumns(session, "ontology_metric")
    expectedMetric = {
        "metric_type",
        "numerator_measure",
        "denominator_measure",
        "aggregation_order",
        "grain_keys",
        "filter_expression",
        "status",
        "owner",
        "execution_source",
    }
    missingMetric = sorted(expectedMetric - metricColumns)
    check(
        "S1.2 ontology_metric 新增 9 列",
        not missingMetric,
        f"缺失 {missingMetric}" if missingMetric else f"实到 {len(expectedMetric)} 列",
    )

    kpiColumns = await tableColumns(session, "kpi_catalog")
    expectedKpi = {"grain_keys", "numerator_columns", "denominator_columns"}
    missingKpi = sorted(expectedKpi - kpiColumns)
    check(
        "S1.3 kpi_catalog 新增 3 列",
        not missingKpi,
        f"缺失 {missingKpi}" if missingKpi else "grain_keys/numerator_columns/denominator_columns",
    )

    sourceColumns = await tableColumns(session, "ontology_metric_source")
    check(
        "S1.4 ontology_metric_source 表存在",
        bool(sourceColumns),
        f"列数 {len(sourceColumns)}",
    )

    uniqueSets = await uniqueConstraintColumnSets(session, "ontology_metric_source")
    wanted = frozenset({"metric_id", "class_id", "role"})
    check(
        "S1.5 UNIQUE(metric_id, class_id, role)",
        wanted in uniqueSets,
        f"实际约束列组合 {[sorted(s) for s in uniqueSets]}",
    )

    check(
        "S1.6 ontology_metric_source.class_id 索引",
        await hasIndexOn(session, "ontology_metric_source", "class_id"),
        "",
    )


# ── S2 回填预览与物理列校验 ───────────────────────────────────────────


async def step2_backfillPreview(session: AsyncSession) -> None:
    """PLAN 与**库内** KPI 交叉核对；tier 分布只作观测（T16-11）。"""
    plans = list(buildPlans())
    planCodes = {plan.kpiCode for plan in plans}
    tiers = dict(Counter(plan.tier() for plan in plans))
    info(
        f"PLAN：{len(plans)} 条，tier 分布 {tiers}"
        f"（构造期望 {EXPECTED_TIER_COUNTS}）"
    )
    if tiers != EXPECTED_TIER_COUNTS:
        info("⚠️ PLAN 的 tier 分布与构造期望不一致 —— 先复核 PLAN，不要调松判据")

    kpis = await fetchKpis(session)
    dbCodes = {kpi.kpi_code for kpi in kpis}
    check(
        "S2.1 库内 kpi_catalog 与 PLAN 的 KPI 集合一致",
        dbCodes == planCodes,
        f"库内 {len(dbCodes)} 条 / PLAN {len(planCodes)} 条；"
        f"缺 {sorted(planCodes - dbCodes)}；多 {sorted(dbCodes - planCodes)}",
    )

    # T16-11 的 S2.2′：库内挂靠必须与 PLAN 的**归属**一致。严格集合相等
    # 在「回填前」不成立（S2 在 S9 之前跑，此时 linked = 0），因此改用可判定的
    # 不变量：**要么 0 条（未回填），要么全量挂靠且 metric_name 与 PLAN 一致**
    # ——不允许「部分挂靠」或「挂错对象」；且挂靠的 code 不得越出 PLAN。
    # 检查名说的是「归属」⇒ 必须真的比对归属键（metric_name），不能只数条数。
    linkedNames = await fetchLinkedMetricNames(session)
    expectedNames = {plan.kpiCode: plan.metricName for plan in plans}
    wrongTargets = sorted(
        code for code, name in linkedNames.items() if expectedNames.get(code) != name
    )
    isConsistent = (
        set(linkedNames) <= planCodes
        and len(linkedNames) in (0, len(planCodes))
        and not wrongTargets
    )
    check(
        "S2.2 库内挂靠与 PLAN 归属一致（0 条=未回填；或全量挂靠且 metric_name 匹配）",
        isConsistent,
        f"已挂靠 {len(linkedNames)} 条 / PLAN {len(planCodes)} 条；"
        f"越界 {sorted(set(linkedNames) - planCodes)}；挂错 {wrongTargets}",
    )
    if not linkedNames:
        info("S2.2 备注：0 条 KPI 已挂靠 ontology_metric（尚未回填）")

    columnIndex = await classColumnIndex(session)
    info(f"schema_cache 覆盖的本体类/表键：{len(columnIndex)} 个")

    degraded: list[str] = []
    for plan in plans:
        missing = verifyPlan(plan, columnIndex)
        if missing:
            degraded.append(f"{plan.kpiCode}({'/'.join(missing)})")
        if plan.tier() == "A":
            check(
                f"S2.3 tier A 物理列齐备 {plan.kpiCode}",
                not missing,
                f"缺失 {missing}" if missing else "",
            )
    info(f"实测物理列缺失条目：{degraded if degraded else '无'}")
    tierC = [plan for plan in plans if plan.tier() == "C"]
    info(
        "tier C 观测：execution_source="
        f"{sorted({plan.executionSource for plan in tierC})}，"
        f"结构化度量条数={sum(1 for p in tierC if p.numerator or p.denominator)}"
    )


# ── S3 三层对账逐档 ───────────────────────────────────────────────────


async def _collectReconcile(
    session: AsyncSession, plans: dict[str, KpiBackfillPlan]
) -> list[ReconcileOutcome]:
    """逐条对账并当场判定 S3.1 / S3.2；返回跨条目判定所需的快照。"""
    outcomes: list[ReconcileOutcome] = []
    for kpi in await fetchKpis(session):
        if kpi.metric_id is None:
            continue
        metric = await session.get(OntologyMetric, kpi.metric_id)
        technique = techniqueOf(metric)
        governance = governanceSpecOf(kpi)
        result = reconcileMetric(governance, technique)
        outcomes.append(
            ReconcileOutcome(
                code=kpi.kpi_code,
                verdict=result.verdict,
                reasons=tuple(result.reasons()),
                columnDiffers=anyColumnDiffers(governance, technique),
            )
        )
        plan = plans.get(kpi.kpi_code)
        tier = _tierOfCode(plans, kpi.kpi_code)
        # ⚠️ PLAN **刻意**保留业务治理列（``governance*``）与技术物理列不同
        # （T15-L）：有已登记偏差 ⇒ 对账**必须**判 mismatch，否则等于「脚本自己
        # 给自己签字」。没有偏差时才按档位判 pass / unknown（这两条分支由
        # ``expectedVerdictFor`` 的单测覆盖 —— 真实 12 条全部有偏差）。
        expected = expectedVerdictFor(
            tier, plan.deviations if plan is not None else ()
        )
        hasDeviation = bool(plan is not None and plan.deviations)
        basis = "PLAN 已登记治理↔物理偏差" if hasDeviation else f"tier {tier} 无已登记偏差"
        check(
            f"S3.1 {kpi.kpi_code} 对账 verdict = {expected}（{basis}）",
            result.verdict == expected,
            f"实际 {result.verdict}；findings={result.reasons()}",
        )
        if tier == "A":
            ok, missing = formulaCoverage(
                technique.structuredColumns(), technique.formula
            )
            check(
                f"S3.2 {kpi.kpi_code} tier A formula 覆盖全部结构化列",
                ok,
                f"缺失 {missing}" if missing else "",
            )
    return outcomes


def _judgeReconcile(
    plans: dict[str, KpiBackfillPlan], outcomes: list[ReconcileOutcome]
) -> None:
    """跨条目判定：mismatch 集合（S3.3）与列级 finding 集合（S3.4）。"""
    verdicts = {outcome.code: outcome.verdict for outcome in outcomes}
    mismatches = {
        code for code, verdict in verdicts.items() if verdict == VERDICT_MISMATCH
    }
    deviationCodes = {
        plan.kpiCode for plan in plans.values() if plan.deviations
    } & set(verdicts)
    check(
        "S3.3 mismatch 集合 = PLAN 已登记偏差集合（不多不少）",
        mismatches == deviationCodes,
        f"多判 {sorted(mismatches - deviationCodes)}；漏判 "
        f"{sorted(deviationCodes - mismatches)}",
    )
    # ⚠️ S3.3 单独不够：12 条 PLAN 的治理粒度（中文业务词）与技术粒度（列名）
    # **永远**不等 ⇒ 每条 findings 都含 ``grain_mismatch`` ⇒ 即便把
    # ``_checkRole`` 的列比对整体删掉，S3.3 依旧全绿。S3.4 按 **finding code**
    # 独立断言：列级 finding 的有无必须与「治理列 vs 技术列」的实际差异一致。
    expected = expectedColumnCodes(outcomes)
    actual = actualColumnCodes(outcomes)
    check(
        "S3.4 列级 finding 集合 = 治理/技术列差异集合（防 _checkRole 列比对被删）",
        expected == actual,
        f"期望 {sorted(expected)}；实际 {sorted(actual)}",
    )
    info(f"实测 verdict 分布：{dict(Counter(verdicts.values()))}")
    if len(mismatches) == len(verdicts):
        info(
            f"⚠️ {len(mismatches)}/{len(verdicts)} 条全部 mismatch ⇒ 契约注入与发布闸门"
            "均被对账拦截，直到业务方审核并对齐治理列（这是设计上的多签互锁，"
            "不是脚本缺陷）"
        )


async def step3_reconcile(session: AsyncSession) -> None:
    plans = {plan.kpiCode: plan for plan in buildPlans()}
    outcomes = await _collectReconcile(session, plans)
    if not outcomes:
        skipStep(
            "S3 三层对账逐档",
            "库内 12 条 KPI 均未挂靠 ontology_metric —— 请先执行 "
            "scripts/kpi_metric_backfill.py（并经业务方审核）",
        )
        return
    _judgeReconcile(plans, outcomes)


# ── S4 发布闸门（实现见 kpi_metric_realdata_s4） ──────────────────────
# 单列模块的原因：S4 是唯一会临时改灰度开关的步骤（运行前后必须一致），
# 与只读步骤分开便于审阅；同时避免入口文件越过 800 行。

# ── S5 契约块注入端到端（实现见 kpi_metric_realdata_inject） ─────────────
# 单列模块的原因：S5 的判据依赖「治理列是否已审核对齐」，并含**写测试库**的
# 审核模拟 ⇒ 与只读步骤分开，避免入口文件越过 800 行且便于单独审阅。

# ── S6 精确 alias 匹配可用性 ──────────────────────────────────────────


async def step6_exactAlias(session: AsyncSession) -> None:
    cache = get_kpi_match_cache()
    await cache.ensureFresh(session)

    check("S6.1 缓存已 warmUp", cache.is_warmUp(), "")
    check("S6.2 缓存含 KPI_SUPPLIER_OTD", cache.hasCode("KPI_SUPPLIER_OTD"), "")

    service = getKpiSemanticMatchService()
    results = await service.matchAll(CONTRACT_QUESTION, limit=5)
    hit = next((r for r in results if r.code == "KPI_SUPPLIER_OTD"), None)
    check(
        "S6.3 精确 alias 命中且 confidence = 1.0",
        hit is not None and hit.confidence == 1.0,
        f"结果 {[(r.code, r.confidence) for r in results]}",
    )

    # 已知限制 L1：自然语言问句的 Jaccard 路径实测不可达（0.09 << 0.75）。
    # 这里**如实打印**实测最高分，不断言（断言「不可达」等于把限制固化成契约）。
    natural = "供应商准时交付率是多少"
    naturalResults = await service.matchAll(natural, limit=5)
    top = naturalResults[0].confidence if naturalResults else 0.0
    info(
        f"L1 遗留项实测：自然语言问句 {natural!r} 最高分 {top} "
        "（目录关键词 5–6 词 vs 阈值 0.75 ⇒ Jaccard 路径不可达）"
    )


# ── S7 缓存动态性 ─────────────────────────────────────────────────────


async def step7_cacheDynamic(session: AsyncSession) -> None:
    cache = get_kpi_match_cache()
    await cache.ensureFresh(session)

    kpis = await fetchKpis(session)
    published = [kpi for kpi in kpis if kpi.status == "PUBLISHED"]
    cached = cache.getAll()
    check(
        "S7.1 缓存条目数 = PUBLISHED KPI 条数",
        len(cached) == len(published),
        f"缓存 {len(cached)} vs PUBLISHED {len(published)}（全量 {len(kpis)}）",
    )

    withKeywords = [kpi for kpi in published if kpi.semantic_keywords]
    if not withKeywords:
        skipStep(
            "S7.2 findByAnyKeyword 命中候选",
            "库里没有带 semantic_keywords 的 PUBLISHED KPI —— 请先跑 "
            "scripts/seed_kpi_semantic_index.py",
        )
        return
    sample = withKeywords[0]
    keyword = (sample.semantic_keywords or [""])[0]
    candidates = cache.findByAnyKeyword([keyword])
    check(
        "S7.2 findByAnyKeyword 命中候选",
        bool(candidates),
        f"关键词 {keyword!r} 命中 {len(candidates)} 条",
    )


# ── S8 data_source 裸表名精确 join ────────────────────────────────────


def _sourceSplitChecks() -> None:
    check(
        "S8.1 拆分纯函数：多表 + 去重 + 大写 + 保序",
        splitLegacyTableNames(
            "DWD_PURCHASE_ORDER + DWD_GOODS_RECEIPT + DWD_PURCHASE_ORDER"
        )
        == ("DWD_PURCHASE_ORDER", "DWD_GOODS_RECEIPT"),
        "",
    )
    check(
        "S8.2 拆分纯函数：空值",
        splitLegacyTableNames(None) == () and splitLegacyTableNames("") == (),
        "",
    )


async def step8_legacyTableJoin(session: AsyncSession) -> None:
    _sourceSplitChecks()

    schemaRows = tuple((await session.execute(select(SchemaCache))).scalars().all())
    index = buildTableIndex(schemaRows)
    check("S8.3 表索引纯函数：空输入", buildTableIndex(()) == {}, "")

    kpis = await fetchKpis(session)
    refs: list[tuple[str, str]] = []
    for kpi in kpis:
        refs.extend((kpi.kpi_code, table) for table in splitLegacyTableNames(kpi.data_source))
    hits = [(code, table) for code, table in refs if table in index]
    missing = [f"{code}:{table}" for code, table in refs if table not in index]
    rate = (len(hits) / len(refs)) if refs else 0.0
    info(
        f"实测：{len(refs)} 个裸表名引用，命中 {len(hits)} 个（{rate:.1%}）；"
        f"未命中 {len(missing)} 个"
    )
    if missing:
        info("未命中清单：" + "、".join(missing[:20]))
    check(
        "S8.4 裸表名 join 有命中（否则信号完全失效）",
        len(hits) > 0,
        f"命中 {len(hits)} / {len(refs)}",
    )


# ── S9 回填落库（仅 --apply-backfill） ────────────────────────────────


async def step9_applyBackfill(session: AsyncSession) -> None:
    """跑两遍回填（默认非破坏性模式），验证幂等。"""
    plans = list(buildPlans())
    before = await countMetrics(session)

    first = await applyPlans(session, plans)
    info(
        f"第 1 次回填：created={len(first.created)} updated={len(first.updated)} "
        f"degraded={len(first.degraded)} skipped={len(first.skipped)} "
        f"unbound={len(first.unbound_data_source)} "
        f"unresolved={len(first.unresolved_classes)}"
    )
    if first.skipped or first.unresolved_classes:
        info(
            "第 1 次回填存在 skipped / unresolved —— 幂等判据会因此不成立（如实暴露）"
        )
    afterFirst = await countMetrics(session)

    second = await applyPlans(session, plans)
    info(
        f"第 2 次回填：created={len(second.created)} updated={len(second.updated)} "
        f"degraded={len(second.degraded)} skipped={len(second.skipped)}"
    )
    check(
        "S9.1 第 2 次回填全部落为 updated（幂等）",
        not second.created and len(second.updated) == len(plans),
        f"created={len(second.created)} updated={len(second.updated)} "
        f"skipped={len(second.skipped)} plans={len(plans)}",
    )
    afterSecond = await countMetrics(session)
    check(
        "S9.2 二次回填不新增指标行",
        afterSecond == afterFirst,
        f"{before} → {afterFirst} → {afterSecond}",
    )

    kpis = await fetchKpis(session)
    unbound = [kpi.kpi_code for kpi in kpis if kpi.metric_id is None]
    check(
        f"S9.3 {EXPECTED_KPI_COUNT} 条 KPI 全部挂靠 ontology_metric",
        not unbound,
        f"未挂靠 {unbound}" if unbound else f"{len(kpis)} 条已挂靠",
    )


# ── 主流程 ────────────────────────────────────────────────────────────


def _buildSteps(applyBackfill: bool) -> list[tuple[str, object]]:
    """步骤清单；回填必须在对账**之前**跑，否则 S3 读到回填前状态。"""
    steps: list[tuple[str, object]] = [
        ("S1", step1_schema),
        ("S2", step2_backfillPreview),
        ("S3", step3_reconcile),
        ("S4", step4_publishGate),
        ("S5", step5_contractInjection),
        ("S6", step6_exactAlias),
        ("S7", step7_cacheDynamic),
        ("S8", step8_legacyTableJoin),
    ]
    if applyBackfill:
        steps.insert(2, ("S9", step9_applyBackfill))
    else:
        # 只读模式**不会**执行 S9 —— 必须显式登记为 SKIP，否则汇总里的
        # ``passed=`` 会把它算成「通过」（9 步里只跑了 8 步）。
        skipStep(
            "S9 回填落库 + 幂等",
            "只读模式未加 --apply-backfill（落库需业务方审核后显式发起）",
        )
    return steps


async def _runSteps(session: AsyncSession, steps: list[tuple[str, object]]) -> None:
    """逐步执行：**单步崩溃记失败并继续**（后续步骤与汇总仍会产出）。"""
    for name, step in steps:
        if name in _SCHEMA_DEPENDENT_STEPS and not _SCHEMA_READY:
            skipStep(
                f"{name} 步骤",
                "迁移未应用到 0137（新列/新表缺失）—— 该步骤在当前库上无法执行",
            )
            continue
        info(f"STEP {name} begin")
        try:
            await step(session)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 —— 单步失败不得吞掉后续证据
            failStep(f"{name} 执行异常", repr(exc))
            # 查询报错后 PG 事务已 aborted ⇒ 不回滚的话后续每一步都会连锁报
            # InFailedSQLTransaction，真实结论被噪声淹没。
            await session.rollback()
        info(f"STEP {name} end")


async def main(argv: list[str] | None = None) -> int:
    applyBackfill = "--apply-backfill" in (argv if argv is not None else sys.argv[1:])
    print(f"TARGET  {maskDsn(DATABASE_URL)}  (from {DSN_SOURCE})")
    print(f"DATABASE  {TARGET_DB}")
    print(f"MODE  {'apply-backfill' if applyBackfill else 'read-only'}")
    print(f"PLAN STEPS = {MAX_STEPS}")
    print("-" * 72)

    # 非生产环境的 engine 默认 SQL echo（``echo=not settings.isProduction``）；
    # echo 走 SQLAlchemy 的 InstanceLogger，**绕过 logger 级别** ⇒ 必须把引擎
    # 自己的 echo 标志关掉，否则证据文件被 SQL 噪声淹没（同
    # ``feat_agent_orchestration_realdata`` 的处理）。
    getEngine().echo = False
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    steps = _buildSteps(applyBackfill)
    sessionFactory = getSessionFactory()
    try:
        async with sessionFactory() as session:
            await _preflight(session)
            await _runSteps(session, steps)
    except Exception as exc:  # noqa: BLE001 —— 建连/提交失败也必须留下汇总
        failStep("MAIN 执行异常", repr(exc))
    finally:
        # ⚠️ 汇总**必须**打印（无论成败）：崩溃被伪装成「没有失败」
        #    是本脚本最坏的失败模式（T16-14）。
        code = printSummary()
    if SKIPS:
        info(f"跳过步骤数 = {len(SKIPS)}；例如：{lastSkipReason('S3')}")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
