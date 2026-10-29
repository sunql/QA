"""KPI 指标技术形态回填/合成脚本（CLI 入口）。

把 12 条 KPI 的**治理口径**反向合成为 ``ontology_metric`` 的**技术形态候选**，
并把 KPI 挂到对应指标上（``kpi_catalog.metric_id``）。

用法::

    # 预览（不写库；需要显式 --database-url）
    ./.venv/bin/python scripts/kpi_metric_backfill.py \\
        --database-url 'postgresql+asyncpg://user:pw@localhost:5434/qa_metadata_test_t15'

    # 落库（测试库免 opt-in）
    ./.venv/bin/python scripts/kpi_metric_backfill.py --apply \\
        --database-url 'postgresql+asyncpg://user:pw@localhost:5434/qa_metadata_test_t15'

    # 正式库：必须 --allow-prod-write；--apply 还会先做当日备份（T15-B）
    ./.venv/bin/python scripts/kpi_metric_backfill.py --allow-prod-write \\
        --database-url 'postgresql+asyncpg://user:pw@localhost:5433/qa_metadata'

四道闸（T15-W）
---------------
1. **显式目标，不猜**：只认 ``--database-url``，不读 ``DATABASE_URL`` /
   ``TEST_DATABASE_URL``（``backend/app/config.py:58`` 的默认值指向 5432 的
   OpenMetadata PG，是陷阱）；
2. **库名白名单**：``scripts.kpi_metric_db_guard``；
3. **prod 写入需显式 opt-in**：目标库是 ``qa_metadata`` 时必须 ``--allow-prod-write``；
4. **prod 写入需备份**：``--apply`` 到 ``qa_metadata`` 时脚本自己先跑 ``pg_dump``
   （当日文件已存在且非空则复用），否则拒绝写。

T15-A：守卫校验的 DSN 与真正落库的 DSN 是**同一个** —— 会话由
``buildSessionFactory(databaseUrl)`` 自建（不用 ``getSessionFactory()``），
落库前再断言 ``SELECT current_database()`` 等于守卫放行的库名。

破坏性语义（FIX-I1）
--------------------
默认**非破坏性**：已存在的指标只填空值、source 行只追加、KPI 治理列只填空值。
``--overwrite`` 才全量重写；该模式下若目标指标当前是 ``PUBLISHED``，写完后
**回退为 ``DRAFT``**（需重新发布过闸）。预览与审核清单都会打印这条提示。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import (  # noqa: E402
    ArgumentError,
    DBAPIError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.domain.models import KpiCatalog, OntologyMetric  # noqa: E402
from scripts.kpi_metric_backfill_apply import (  # noqa: E402
    ApplySummary,
    applyPlans,
    classColumnIndex,
    classIdByTable,
    verifyPlan,
)
from scripts.kpi_metric_backfill_plan import (  # noqa: E402
    PLAN,
    KpiBackfillPlan,
    MeasurePlan,
    buildPlans,
    defaultMetricStatus,
)
from scripts.kpi_metric_db_guard import (  # noqa: E402
    ALLOWED_DB_PATTERN,
    PROD_DB_NAME,
    assertSafeTarget,
    databaseNameOf,
    maskDsn,
)

__all__ = [
    "PLAN",
    "ApplySummary",
    "KpiBackfillPlan",
    "MeasurePlan",
    "PreviewContext",
    "assertSafeTarget",
    "buildPlans",
    "buildSessionFactory",
    "classColumnIndex",
    "databaseNameOf",
    "defaultMetricStatus",
    "main",
    "maskDsn",
    "renderPreview",
    "renderReviewChecklist",
    "verifyPlan",
    "ALLOWED_DB_PATTERN",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKUP_DIR = Path("/tmp")
_BACKUP_TABLES = ("ontology_metric", "ontology_metric_source", "kpi_catalog")
_SQLSTATE_UNDEFINED = ("42703", "42P01")


@dataclass(frozen=True)
class PreviewContext:
    """预览所需的目标库事实（默认空 ⇒ 纯函数预览，供单测调用）。"""

    existingMetricNames: frozenset[str] = frozenset()
    matchedKpiCodes: frozenset[str] = frozenset()
    degraded: tuple[tuple[str, tuple[str, ...]], ...] = ()
    unresolvedClasses: tuple[tuple[str, tuple[str, ...]], ...] = ()
    publishedMetricNames: frozenset[str] = frozenset()

    def isExisting(self, plan: KpiBackfillPlan) -> bool:
        return plan.kpiCode in self.matchedKpiCodes or (
            plan.metricName in self.existingMetricNames
        )


def buildSessionFactory(databaseUrl: str) -> async_sessionmaker[AsyncSession]:
    """按**守卫放行的那个 DSN** 建会话工厂（T15-A：DSN 单一来源）。

    ``expire_on_commit=False`` 与测试支撑（``app/tests/_pg_support.py``）一致：
    commit 后仍可读实体属性，便于汇总输出。
    """
    engine = create_async_engine(databaseUrl, echo=False, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _tierCounts(plans: Sequence[KpiBackfillPlan]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan.tier()] = counts.get(plan.tier(), 0) + 1
    return counts


def _issueMap(
    items: Sequence[tuple[str, Sequence[str]]],
) -> dict[str, tuple[str, ...]]:
    return {code: tuple(values) for code, values in items}


def renderPreview(
    plans: Sequence[KpiBackfillPlan], context: PreviewContext | None = None
) -> str:
    """预览文本（默认模式输出）。

    **两类计数分开报**（dispatch 要求）：「新建本体指标」与「匹配到已有指标」。
    """
    ctx = context or PreviewContext()
    existing = [plan for plan in plans if ctx.isExisting(plan)]
    fresh = [plan for plan in plans if not ctx.isExisting(plan)]
    tiers = _tierCounts(plans)

    lines = ["# KPI 指标技术形态回填预览", ""]
    lines.append(f"共 {len(plans)} 条。**默认预览不写库**；落库请加 `--apply`。")
    lines.append(f"- 新建本体指标：{len(fresh)} 条")
    lines.append(f"- 匹配到已有指标（默认只填空值；`--overwrite` 才覆盖）：{len(existing)} 条")
    lines.append(
        "- 档位分布：" + " / ".join(f"{tier} {tiers[tier]}" for tier in sorted(tiers))
    )
    lines.append(
        "- `--overwrite` 语义：全量重写 + 删除 plan 之外的 source 行；"
        "**已 PUBLISHED 的指标会被回退为 DRAFT，需重新发布过闸**。"
    )
    published = sorted(
        plan.metricName
        for plan in existing
        if plan.metricName in ctx.publishedMetricNames
    )
    if published:
        lines.append(
            "- ⚠️ 以下已存在且已 PUBLISHED 的指标，在 `--overwrite` 下会回退为 "
            "DRAFT（需重新发布过闸）：" + "、".join(published)
        )
    lines.append("")

    degraded = _issueMap(ctx.degraded)
    unresolved = _issueMap(ctx.unresolvedClasses)
    for plan in plans:
        lines.append(f"## {plan.kpiCode}（{plan.tier()} 档）")
        lines.append(f"- 指标名：{plan.metricName}")
        lines.append(f"- 落库动作：{'更新已有指标' if ctx.isExisting(plan) else '新建指标'}")
        lines.append(f"- 类型：{plan.metricType} / 聚合阶：{plan.aggregationOrder}")
        lines.append(f"- 分子：{_measureText(plan.numerator)}")
        lines.append(f"- 分母：{_measureText(plan.denominator)}")
        lines.append(f"- 技术粒度：{list(plan.grainKeys)}")
        lines.append(f"- 治理粒度（业务原声明）：{list(plan.governanceGrain)}")
        lines.append(f"- execution_source：{plan.executionSource}")
        lines.append(f"- 类映射：{list(plan.classRefs)}")
        if plan.kpiCode in degraded:
            lines.append(f"- ⚠️ 物理列缺失（将降级）：{'、'.join(degraded[plan.kpiCode])}")
        if plan.kpiCode in unresolved:
            lines.append(f"- ⚠️ 本体类未解析（将跳过）：{'、'.join(unresolved[plan.kpiCode])}")
        lines.append("")
    return "\n".join(lines)


def _measureText(measure: MeasurePlan | None) -> str:
    return measure.toRaw().__repr__() if measure else "（无结构化度量）"


_REVIEW_PREAMBLE: tuple[str, ...] = (
    "# KPI 指标技术形态 —— 业务方审核清单",
    "",
    "回填脚本产出的是**候选技术形态**，不是结论。",
    "每条指标都是 `DRAFT`；发布闸门要求 `metric.status = PUBLISHED` ⇒",
    "**业务方核对本清单后，在本体指标页手工发布**，KPI 才能发布。",
    "",
    "## 脚本不做的事（必须人工补）",
    "",
    "- **不绑定数据源**：`kpi_catalog.datasource_id` 保持原值。",
    "- **不改 KPI 发布状态**：脚本只写 `metric_id` 与结构化列。",
    "- **不代业务确认口径**：下方每条的「需确认的偏差」逐条核对。",
    "- **不改治理列**：`num/denominator_columns` 写的是**业务原声明**。",
    "  层 2 对账因此判 **mismatch**，这是**预期**（防「脚本自己给自己签字」）。",
    "",
    "## 落库后的连带影响（上线前必读）",
    "",
    "回填不只是写数据，它会**改变 NL2SQL 的运行时行为**：",
    "",
    "- 脚本会把 `kpi_catalog.metric_id` 挂到本体指标上。",
    "- Tier B 契约注入（`backend/app/services/kpi_contract_builder.py`）要求 KPI",
    "  挂到本体指标才会注入契约块 ⇒ **`metric_id` 一落库，契约注入就有了注入对象**。",
    "- 而 `KPI_CONTRACT_INJECTION_ENABLED` 开关在本分支**默认 true**",
    "  （`backend/app/services/kpi_metric_switches.py`；`system_config` 无该行时",
    "  `readKpiMetricSwitch(session, SWITCH_CONTRACT_INJECTION, True)` 返回 `True`）",
    "  ⇒ **回填落库后，契约注入默认生效，不需要额外翻开关**。",
    "- 对比：`KPI_METRIC_GATE_ENABLED`（发布闸门）默认 **false** ⇒ 闸门「先观察再阻断」；",
    "  注入则是「默认开」。**两者灰度口径不一致，上线前要单独决策。**",
    "",
    "**因此业务方/运维在 `--apply` 之前必须二选一（写进清单，不要口头传）：**",
    "",
    "1. **先关后开**：先把 `KPI_CONTRACT_INJECTION_ENABLED` 显式置为 `false`，",
    "   回填落库后观察一轮，再按需置 `true`；",
    "2. **接受默认生效**：直接回填，契约注入随之启用（NL2SQL plan prompt 会多出",
    "   「【KPI 口径契约】」块）。",
    "",
    "> 把注入显式置 false 的写法（**控制器上线决策，不由脚本默认执行**）：",
    "> 在 `system_config` 写 `config_key='KPI_CONTRACT_INJECTION_ENABLED'`、",
    "> `config_value='false'`（按 `seed_system_config.py` 的 upsert 口径）。",
    "",
    "## `--overwrite` 的破坏性语义（默认关闭，FIX-I1）",
    "",
    "- **默认（不带 `--overwrite`）**：对已存在的指标 / 来源 / KPI 治理列**只填空值**，",
    "  已有非空值一律不覆盖；也**不删除**任何 source 行 ⇒ 人工修订过的公式、owner、",
    "  治理列不会被脚本回退。",
    "- **`--overwrite`**：全量重写 + 删除 plan 之外的 source 行。",
    "  **若目标指标当前是 `PUBLISHED`，写完后会被回退为 `DRAFT`，必须重新发布并",
    "  重新过发布闸门**（否则契约注入口径会与指标本体不一致）。",
    "",
    "## 正式库备份（T15-B，不可跳过）",
    "",
    "`--apply` 到 `qa_metadata`（prod 同源）时脚本会**自己先跑 pg_dump**：",
    "`/tmp/<table>_<YYYYMMDD>_<alembic_revision>.sql`（`ontology_metric` / "
    "`ontology_metric_source` / `kpi_catalog`），口令走 `PGPASSWORD` 环境变量、不进 argv。",
    "备份先写 `.tmp` 再原子替换；产物不存在或为空 ⇒ 脚本**拒绝写入**。",
    "",
    "## 逐条核对",
    "",
)


def renderReviewChecklist(
    plans: Sequence[KpiBackfillPlan], context: PreviewContext | None = None
) -> str:
    """上线前人工核对清单（业务方核对后手工发布 ⇒ 发布闸门才放行）。"""
    ctx = context or PreviewContext()
    lines = list(_REVIEW_PREAMBLE)
    for plan in plans:
        lines.extend(_checklistFor(plan, ctx))
    lines.extend(_checklistSummary(plans, ctx))
    return "\n".join(lines)


def _checklistFor(plan: KpiBackfillPlan, ctx: PreviewContext) -> list[str]:
    lines = [
        f"### {plan.kpiCode} —— {plan.metricName}",
        f"- 档位：{plan.tier()}",
        f"- 落库动作：{'更新已有指标' if ctx.isExisting(plan) else '新建指标'}",
        f"- 技术粒度键：{list(plan.grainKeys)}",
        f"- 治理粒度（业务原声明）：{list(plan.governanceGrain)}",
        f"- 治理分子列（业务原声明）：{list(plan.governanceNumerator)}",
        f"- 治理分母列（业务原声明）：{list(plan.governanceDenominator)}",
        f"- 结构化分子（物理列）：{_measureText(plan.numerator)}",
        f"- 结构化分母（物理列）：{_measureText(plan.denominator)}",
        f"- 可执行形态 / 口径声明：`{plan.formula}`",
        "- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）",
        "- **需确认的偏差（治理列 ↔ 物理列）**：",
    ]
    lines.extend(f"  - {item}" for item in plan.deviations)
    lines.append("")
    return lines


def _checklistSummary(
    plans: Sequence[KpiBackfillPlan], ctx: PreviewContext
) -> list[str]:
    lines = ["## 汇总", ""]
    lines.extend(
        f"- {tier} 档（{count} 条）"
        for tier, count in sorted(_tierCounts(plans).items())
    )
    existing = sum(1 for plan in plans if ctx.isExisting(plan))
    lines.append(f"- 新建本体指标：{len(plans) - existing} 条")
    lines.append(f"- 匹配到已有指标：{existing} 条")
    if ctx.degraded:
        lines.append("- 物理列缺失将降级：" + "、".join(code for code, _ in ctx.degraded))
    if ctx.unresolvedClasses:
        lines.append(
            "- 本体类未解析将跳过：" + "、".join(code for code, _ in ctx.unresolvedClasses)
        )
    lines.append("")
    return lines


def _requireDatabaseUrl(explicit: str | None) -> str:
    """闸 1：显式目标，不猜（不读 env 默认值 —— 5432 是 OpenMetadata）。"""
    if not explicit:
        raise SystemExit(
            "缺少目标库：必须显式传 --database-url（不读 DATABASE_URL / "
            "TEST_DATABASE_URL —— 本机 5432 的默认值指向 OpenMetadata 的 PG）。"
        )
    return explicit


def _selectPlans(only: str | None) -> list[KpiBackfillPlan]:
    plans = list(buildPlans())
    if not only:
        return plans
    wanted = {code.strip() for code in only.split(",") if code.strip()}
    return [plan for plan in plans if plan.kpiCode in wanted]


def _isMissingStatusColumn(exc: ProgrammingError) -> bool:
    """仅识别「0135 的 prod 没有 ontology_metric.status」这一种缺列（其余照抛）。"""
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate == "42703" and "status" in str(exc).lower()


async def _metricFacts(session: AsyncSession) -> tuple[frozenset[str], frozenset[str]]:
    """读指标名 + PUBLISHED 集合。

    ``ontology_metric.status`` 是 0136 才加的列；针对 0135 的 prod 只读预览时
    退化为「读名字、PUBLISHED 集合为空」并明示，**不把缺列当成无指标**。
    """
    try:
        rows = (
            await session.execute(
                select(OntologyMetric.metric_name, OntologyMetric.status)
            )
        ).all()
    except ProgrammingError as exc:
        if not _isMissingStatusColumn(exc):
            raise
        await session.rollback()
        print(
            "⚠️ 目标库无 ontology_metric.status（迁移 0136 未应用）⇒ "
            "本次预览不判定 PUBLISHED，`--overwrite` 前请自行确认发布状态"
        )
        names = (
            await session.execute(select(OntologyMetric.metric_name))
        ).scalars().all()
        return frozenset(names), frozenset()
    return (
        frozenset(name for name, _status in rows),
        frozenset(name for name, status in rows if status == "PUBLISHED"),
    )


async def _previewContext(
    session: AsyncSession, plans: Sequence[KpiBackfillPlan]
) -> PreviewContext:
    """读目标库事实（**只 select 旧库也有的列** ⇒ 0135 的 prod 也能 dry-run）。"""
    names, published = await _metricFacts(session)
    kpiRows = (
        await session.execute(
            select(KpiCatalog.kpi_code, KpiCatalog.metric_id)
        )
    ).all()
    matched = frozenset(code for code, metricId in kpiRows if metricId is not None)
    columnIndex = await classColumnIndex(session)
    byTable, ambiguous = await classIdByTable(session)

    degraded: list[tuple[str, tuple[str, ...]]] = []
    unresolved: list[tuple[str, tuple[str, ...]]] = []
    for plan in plans:
        missing = verifyPlan(plan, columnIndex)
        if missing:
            degraded.append((plan.kpiCode, missing))
        bad = [
            f"{table}（{role}）"
            for table, role in plan.classRefs
            if table.rsplit(".", 1)[-1].upper() not in byTable
            or table.rsplit(".", 1)[-1].upper() in ambiguous
        ]
        if bad:
            unresolved.append((plan.kpiCode, tuple(bad)))
    return PreviewContext(
        existingMetricNames=names,
        matchedKpiCodes=matched,
        degraded=tuple(degraded),
        unresolvedClasses=tuple(unresolved),
        publishedMetricNames=published,
    )


def _backupPath(table: str, revision: str) -> Path:
    """备份文件名带当日日期 + alembic revision（M-4：半截/旧版本备份可辨识）。"""
    safeRevision = revision.strip() or "unknown"
    return _BACKUP_DIR / f"{table}_{date.today():%Y%m%d}_{safeRevision}.sql"


def _dumpTarget(databaseUrl: str) -> tuple[list[str], dict[str, str]]:
    """把 DSN 拆成 argv 前缀 + 环境变量（FIX-I2：**口令永不进 argv**）。"""
    try:
        url = make_url(databaseUrl)
    except ArgumentError as exc:
        raise SystemExit(
            f"备份要求可解析的 PostgreSQL DSN，当前为：{maskDsn(databaseUrl)}"
        ) from exc
    if not url.drivername.startswith("postgresql") or not url.database:
        raise SystemExit(f"备份要求 PostgreSQL DSN，当前为：{maskDsn(databaseUrl)}")
    command = [
        "pg_dump",
        "-h",
        url.host or "localhost",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "",
        "-d",
        url.database,
    ]
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return command, env


def _pgDump(databaseUrl: str, table: str, path: Path) -> None:
    """单表备份：先写 ``.tmp`` 再原子替换（M-4：半截 dump 不会被当成合法备份）。"""
    command, env = _dumpTarget(databaseUrl)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("wb") as handle:
            result = subprocess.run(
                [*command, "-t", table], stdout=handle, stderr=subprocess.PIPE, env=env
            )
    except FileNotFoundError as exc:
        tmp.unlink(missing_ok=True)
        raise SystemExit("备份失败：找不到 pg_dump（写正式库必须先备份）。") from exc
    if result.returncode == 0:
        os.replace(tmp, path)
        return
    tmp.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    stderr = result.stderr.decode("utf-8", "replace")
    if "no matching tables" in stderr:
        print(f"⚠️ 备份跳过 {table}：目标库尚无该表（迁移未应用）")
        return
    raise SystemExit(f"备份失败（{table}）：{stderr[-500:]}")


async def _alembicRevision(databaseUrl: str) -> str:
    """读目标库的 alembic revision（只读；读不到 ⇒ ``unknown``，不阻断备份）。"""
    factory = buildSessionFactory(databaseUrl)
    try:
        async with factory() as session:
            try:
                revision = (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
            except (ProgrammingError, OperationalError):
                print("⚠️ 读不到 alembic_version，备份文件名使用 revision=unknown")
                return "unknown"
            return str(revision)
    finally:
        await factory.kw["bind"].dispose()


def _ensureProdBackup(databaseUrl: str, revision: str) -> tuple[Path, ...]:
    """闸 4：写正式库前必须完成当日备份（存在且非空则复用）⇒ T15-B。"""
    paths: list[Path] = []
    for table in _BACKUP_TABLES:
        path = _backupPath(table, revision)
        if not path.exists() or path.stat().st_size == 0:
            _pgDump(databaseUrl, table, path)
        if path.exists() and path.stat().st_size > 0:
            paths.append(path)
    if len(paths) != len(_BACKUP_TABLES):
        raise SystemExit("备份不完整 ⇒ 拒绝写正式库。")
    return tuple(paths)


def _printBanner(targetDb: str, backups: Sequence[Path]) -> None:
    print("=" * 72)
    print(f"⚠️ 正在写正式库 {targetDb}（prod 同源）")
    for path in backups:
        print(f"   备份：{path}（{path.stat().st_size} bytes）")
    print("=" * 72)


def _printSummary(summary: ApplySummary, targetDb: str) -> None:
    print(
        f"[{targetDb}] 落库完成：created={len(summary.created)} "
        f"updated={len(summary.updated)} degraded={len(summary.degraded)} "
        f"skipped={len(summary.skipped)}"
    )
    if summary.degraded:
        print(f"⚠️ 因物理列缺失降级：{'、'.join(summary.degraded)}")
    if summary.skipped:
        print(f"⚠️ 已跳过（KPI 或本体类不存在）：{'、'.join(summary.skipped)}")
    if summary.unresolved_classes:
        print(f"⚠️ 未解析的本体类：{'；'.join(summary.unresolved_classes)}")
    if summary.unbound_data_source:
        print(
            f"ℹ️ 未绑定数据源的 KPI（{len(summary.unbound_data_source)} 条，"
            "发布闸门会拦，需人工绑定）：" + "、".join(summary.unbound_data_source)
        )
    if summary.reverted_to_draft:
        print(
            "⚠️ 以下指标原为 PUBLISHED，`--overwrite` 已将其回退为 DRAFT，"
            "**需重新发布并重新过发布闸门**：" + "、".join(summary.reverted_to_draft)
        )


def _schemaGapHint(exc: ProgrammingError) -> str | None:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    text_ = f"{exc}"
    if sqlstate in _SQLSTATE_UNDEFINED or any(
        marker in text_ for marker in ("UndefinedColumn", "UndefinedTable", "does not exist")
    ):
        return (
            "目标库缺少本分支的迁移（0136/0137 的列或表不存在）⇒ 先执行 "
            "`DATABASE_URL=<该库> ./.venv/bin/python -m alembic upgrade head`。\n"
            f"原始错误：{text_.splitlines()[0] if text_ else exc}"
        )
    return None


def _scrubSecrets(message: str, databaseUrl: str) -> str:
    """错误文本里若出现 DSN 口令则打码（FIX-I2 的同类风险：日志泄密）。"""
    try:
        password = make_url(databaseUrl).password
    except (ArgumentError, ValueError):
        return message
    if password:
        return message.replace(str(password), "***")
    return message


def connectionErrorExit(exc: BaseException, databaseUrl: str) -> SystemExit:
    """FIX-I4：连接类失败统一转 SystemExit（不吐 traceback、不带口令）。"""
    root = getattr(exc, "orig", None) or exc
    detail = _scrubSecrets(str(root), databaseUrl)
    return SystemExit(f"❌ 连接目标库失败（{maskDsn(databaseUrl)}）：{detail}")


async def _run(args: argparse.Namespace) -> int:
    databaseUrl = _requireDatabaseUrl(args.database_url)
    targetDb = assertSafeTarget(databaseUrl, allowProdWrite=args.allow_prod_write)
    plans = _selectPlans(args.only)

    backups: tuple[Path, ...] = ()
    if args.apply and targetDb == PROD_DB_NAME:
        revision = await _alembicRevision(databaseUrl)
        backups = _ensureProdBackup(databaseUrl, revision)
        _printBanner(targetDb, backups)

    print(f"目标库：{targetDb}（{maskDsn(databaseUrl)}）")
    factory = buildSessionFactory(databaseUrl)
    try:
        async with factory() as session:
            await _assertSameDatabase(session, targetDb)
            context = await _previewContext(session, plans)
            print(renderPreview(plans, context))
            _writeChecklist(args.out, renderReviewChecklist(plans, context))
            if not args.apply:
                print("预览模式：未写库。确认无误后加 `--apply` 落库。")
                return 0
            summary = await applyPlans(
                session,
                plans,
                overwrite=args.overwrite,
                syncEmbedding=args.embed,
            )
            _printSummary(summary, targetDb)
            return 0
    except ProgrammingError as exc:
        hint = _schemaGapHint(exc)
        if hint is None:
            raise
        print(f"❌ {hint}")
        return 2
    except (OperationalError, OSError, DBAPIError, asyncpg.PostgresError) as exc:
        raise connectionErrorExit(exc, databaseUrl) from exc
    finally:
        await factory.kw["bind"].dispose()


async def _assertSameDatabase(session: AsyncSession, allowedDbName: str) -> None:
    """T15-A 核心：守卫放行的库名必须等于会话**真正连的**库。"""
    actual = (await session.execute(text("SELECT current_database()"))).scalar_one()
    if actual != allowedDbName:
        raise RuntimeError(
            f"落库目标与守卫放行目标不一致：{actual!r} != {allowedDbName!r}"
        )


def _writeChecklist(out: str | None, content: str) -> None:
    if not out:
        return
    path = Path(out)
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"审核清单已写入：{path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="KPI 指标技术形态回填/合成脚本（默认预览，--apply 才落库）"
    )
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "破坏性更新：全量重写已存在指标 + 删除 plan 之外的 source 行；"
            "PUBLISHED 指标会被回退为 DRAFT（默认只填空值）"
        ),
    )
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help="允许目标是正式库 qa_metadata（写前自动备份）",
    )
    parser.add_argument("--embed", action="store_true", help="同时同步指标向量（默认关闭）")
    parser.add_argument("--out", default=None, help="审核清单输出路径（markdown）")
    parser.add_argument("--only", default=None, help="只处理指定 KPI（逗号分隔）")
    parser.add_argument(
        "--database-url", default=None, help="目标库 DSN（必填，不读环境变量）"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
