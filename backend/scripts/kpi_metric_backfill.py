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
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

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
)

from app.domain.models import KpiCatalog, OntologyMetric  # noqa: E402
from scripts.db_snapshot_guard import (  # noqa: E402
    assertSameDatabase,
    backupPath,
    dumpTarget,
    ensureBackupDir,
    ensureBackups,
    maskDatabaseUrl,
    openExclusive,
    pgDump,
    resolveDatabaseUrl,
    safeRevision,
    scrubSecrets,
    targetIdentity,
)
from scripts.db_snapshot_guard import (
    buildSessionFactory as _buildSessionFactory,
)
from scripts.db_snapshot_guard import (
    connectionErrorExit as _connectionErrorExit,
)
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
# 备份目录：**私有**（0700），位于系统临时目录下，跨运行稳定 ⇒ 当日备份可复用。
_BACKUP_DIR = Path(tempfile.gettempdir()) / "kpi_metric_backups"
_BACKUP_TABLES = ("ontology_metric", "ontology_metric_source", "kpi_catalog")
_SQLSTATE_UNDEFINED = ("42703", "42P01")

# 「prod 缺 ontology_metric.status」专用判据：必须限定表名 + 列名。
# 裸 "status" 会命中任何 SQL 文本里带 status 的缺列错误（如 grain_keys 缺失）。
_MISSING_STATUS_COLUMN = re.compile(r'"?ontology_metric"?\s*\.\s*"?status"?')


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

    实现已上收到 ``scripts/db_snapshot_guard.buildSessionFactory``（与语义发布
    脚本共用同一份原语）：``expire_on_commit=False`` 与测试支撑
    （``app/tests/_pg_support.py``）一致 —— commit 后仍可读实体属性，便于汇总输出。
    """
    return _buildSessionFactory(databaseUrl)


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
    "- 契约注入（chat 侧 Tier B 面 `backend/app/services/kpi_contract_builder.py`、",
    "  nl2sql 侧 B3 治理契约面 `backend/app/services/kpi_contract_reconcile.py`）",
    "  都要求 KPI 挂到本体指标才有注入对象 ⇒ **`metric_id` 一落库，注入就有了对象**。",
    "- 而 `KPI_CONTRACT_INJECTION_ENABLED` 开关**默认关（fail-closed，2026-10-30 起）**：",
    "  读 `system_config` 行，行缺失 / 值非法 / 读取异常 ⇒ 一律不注入",
    "  （`backend/app/services/kpi_metric_switches.py` 的",
    "  `isKpiContractInjectionEnabled` ⇒ `readKpiMetricSwitch(session, SWITCH_CONTRACT_INJECTION, False)`）",
    "  ⇒ **回填落库后注入仍不生效，必须显式把该行写成 `true` 才会开**。",
    "- 对比：`KPI_METRIC_GATE_ENABLED`（发布闸门）同样默认 **false** ⇒ 两者灰度口径一致，",
    "  都是「先观察再阻断」。",
    "",
    "**因此业务方/运维在 `--apply` 之前要做一次明确决策（写进清单，不要口头传）：**",
    "",
    "1. **先关后开（推荐）**：回填落库（注入保持默认关）→ 业务方审核对齐治理列 →",
    "   观察一轮 → 再把 `KPI_CONTRACT_INJECTION_ENABLED` 显式置为 `true`；",
    "2. **回填即开**：在 `--apply` 的同时把该行写成 `true` ⇒ 契约注入随之启用",
    "   （NL2SQL plan prompt 会多出「【KPI 口径契约】」块）。",
    "",
    "> 把注入置 true / false 的写法（**控制器上线决策，不由脚本默认执行**）：",
    "> 在 `system_config` 写 `key='KPI_CONTRACT_INJECTION_ENABLED'`、",
    "> `value='true'`（或 `'false'`）——列名是 `key` / `value`（不是 `config_key` /",
    "> `config_value`；见 `backend/app/models/system_config.py`），写法按",
    "> `seed_system_config.py` 的 upsert 口径。",
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


def _requireDatabaseUrl(explicit: str | None, envName: str | None = None) -> str:
    """闸 1：显式目标，不猜（不读 env 默认值 —— 5432 是 OpenMetadata）。

    **解析逻辑已上收到** ``scripts/db_snapshot_guard.resolveDatabaseUrl``：
    本脚本原来**自己抄了一份**（显式优先 / 环境变量 / query 不得改目标）⇒
    闸门有两份实现，将来给共享版加一条硬约束时这份不会跟着改 ⇒ 行为分叉
    且无测试能发现。

    本脚本与共享解析器的**唯一差异**是 ``--database-url`` 允许带口令
    （运维既有契约）⇒ 用显式的 ``allowInlinePassword=True`` 开关表达，
    而不是复制一份实现。放开的是**口令**，不是目标：query 改目标、
    没写主机这两道闸照旧拒绝。
    """
    databaseUrl = resolveDatabaseUrl(explicit, envName, allowInlinePassword=True)
    if explicit and _hasInlinePassword(databaseUrl):
        print(
            "⚠️ --database-url 里带了口令 ⇒ 口令会进 ps / shell 历史 / CI 日志；"
            "建议改用 --database-url-env <环境变量名>。"
        )
    return databaseUrl


def _hasInlinePassword(databaseUrl: str) -> bool:
    """DSN 的 userinfo 里是否带口令（只用于提示，不用于拒绝）。"""
    try:
        return bool(make_url(databaseUrl).password)
    except (ArgumentError, ValueError):
        return False


def _selectPlans(only: str | None) -> list[KpiBackfillPlan]:
    plans = list(buildPlans())
    if not only:
        return plans
    wanted = {code.strip() for code in only.split(",") if code.strip()}
    return [plan for plan in plans if plan.kpiCode in wanted]


def _isMissingStatusColumn(exc: ProgrammingError) -> bool:
    """仅识别「0135 的 prod 没有 ontology_metric.status」这一种缺列（其余照抛）。"""
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate != "42703":
        return False
    orig = getattr(exc, "orig", None)
    text_ = str(orig) if orig is not None else ""
    # 真实报错的 SQL 文本在 str(exc) 里（orig 只有 'column "status" does not exist'），
    # 两处都搜：判据必须限定 `ontology_metric.status`，不能是裸 status。
    return bool(_MISSING_STATUS_COLUMN.search(f"{text_}\n{exc}"))


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


def _backupDir() -> Path:
    """创建（或复用）**私有**备份目录；权限只收紧不放宽。"""
    return ensureBackupDir(_BACKUP_DIR)


def _targetIdentity(databaseUrl: str) -> str:
    """从 DSN 派生 ``host_port_db``，清洗成 ``[A-Za-z0-9_.-]``。

    dev 与 prod 同名 ``qa_metadata`` ⇒ 文件名必须带 host/port 才能区分目标库。
    """
    return targetIdentity(databaseUrl)


def _safeRevision(revision: str) -> str:
    """revision 只允许 ``[A-Za-z0-9_.-]``（防止 ``/``、``..`` 拼进路径）。"""
    return safeRevision(revision)


def _backupPath(table: str, revision: str, databaseUrl: str) -> Path:
    """备份文件名带日期 + **目标库身份** + revision（跨库/旧版本备份均可辨识）。"""
    return backupPath(_backupDir(), table, revision, databaseUrl)


def _openExclusive(path: Path) -> BinaryIO:
    """``O_EXCL | O_NOFOLLOW`` 打开文件：已存在 / 符号链接 ⇒ 失败，不覆盖。"""
    return openExclusive(path)


def _dumpTarget(databaseUrl: str) -> tuple[list[str], dict[str, str]]:
    """把 DSN 拆成 argv 前缀 + 环境变量（FIX-I2：**口令永不进 argv**）。"""
    return dumpTarget(databaseUrl)


def _pgDump(databaseUrl: str, table: str, path: Path) -> None:
    """单表备份：先写 ``.tmp`` 再原子替换（M-4：半截 dump 不会被当成合法备份）。

    实现已上收到 ``scripts/db_snapshot_guard.pgDump``（与语义发布脚本共用同一份
    闸门原语）：多了 ``pg_dump`` 超时与 ``stdin=DEVNULL`` —— DSN 里没口令而服务端
    要口令时，进程会挂在交互式提示上，整个流水线静默卡死。
    """
    pgDump(databaseUrl, table, path)


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
    """闸 4：写正式库前必须完成当日备份（存在且非空则复用）⇒ T15-B。

    文件名含目标库身份（host/port/db）⇒ 复用只可能复用**同一目标库**的备份。

    实现已上收到 ``scripts/db_snapshot_guard.ensureBackups``（与语义发布脚本
    共用同一份闸门原语）：本脚本原来**自己抄了一份**（软链拒绝 / 缺失即 dump /
    数量不齐即拒绝写）⇒ 闸门有两份实现，将来给共享版加一条硬约束时这份不会
    跟着改 ⇒ 行为分叉且无测试能发现。
    """
    return ensureBackups(databaseUrl, _BACKUP_TABLES, revision, _BACKUP_DIR)


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


def _schemaGapHint(exc: ProgrammingError, databaseUrl: str) -> str | None:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    text_ = f"{exc}"
    if sqlstate in _SQLSTATE_UNDEFINED or any(
        marker in text_ for marker in ("UndefinedColumn", "UndefinedTable", "does not exist")
    ):
        detail = text_.splitlines()[0] if text_ else str(exc)
        # 原始错误可能回显连接串 / 口令 ⇒ 打码后再展示
        return (
            "目标库缺少本分支的迁移（0136/0137 的列或表不存在）⇒ 先执行 "
            "`DATABASE_URL=<该库> ./.venv/bin/python -m alembic upgrade head`。\n"
            f"原始错误：{_scrubSecrets(detail, databaseUrl)}"
        )
    return None


def _scrubSecrets(message: str, databaseUrl: str) -> str:
    """错误文本里若出现 DSN 口令则打码（FIX-I2 的同类风险：日志泄密）。"""
    return scrubSecrets(message, databaseUrl)


def connectionErrorExit(exc: BaseException, databaseUrl: str) -> SystemExit:
    """FIX-I4：连接类失败统一转 SystemExit（不吐 traceback、不带口令）。"""
    return _connectionErrorExit(exc, databaseUrl)


async def _run(args: argparse.Namespace) -> int:
    databaseUrl = _requireDatabaseUrl(args.database_url, args.database_url_env)
    targetDb = assertSafeTarget(databaseUrl, allowProdWrite=args.allow_prod_write)
    plans = _selectPlans(args.only)

    backups: tuple[Path, ...] = ()
    if args.apply and targetDb == PROD_DB_NAME:
        revision = await _alembicRevision(databaseUrl)
        backups = _ensureProdBackup(databaseUrl, revision)
        _printBanner(targetDb, backups)

    print(f"目标库：{targetDb}（{maskDatabaseUrl(databaseUrl)}）")
    factory = buildSessionFactory(databaseUrl)
    try:
        async with factory() as session:
            await _assertSameDatabase(session, targetDb, databaseUrl)
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
        hint = _schemaGapHint(exc, databaseUrl)
        if hint is None:
            raise
        print(f"❌ {hint}")
        return 2
    except (OperationalError, OSError, DBAPIError, asyncpg.PostgresError) as exc:
        raise connectionErrorExit(exc, databaseUrl) from exc
    finally:
        await factory.kw["bind"].dispose()


async def _assertSameDatabase(
    session: AsyncSession,
    allowedDbName: str,
    databaseUrl: str,
) -> None:
    """T15-A 核心：守卫放行的库名必须等于会话**真正连的**库。

    实现已上收到 ``scripts.db_snapshot_guard.assertSameDatabase``：那边抛
    ``SystemExit``（带打码 DSN、干净退出、不吐裸 traceback）。本脚本原来抛
    ``RuntimeError`` ⇒ 同样的事故在两个脚本里表现不同（一个干净退出、一个
    裸栈 + 退出码 1）⇒ 「共享原语名义上共享、行为上分叉」。
    """
    await assertSameDatabase(session, allowedDbName, databaseUrl)


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
        "--database-url", default=None, help="目标库 DSN（必填；口令建议改用 --database-url-env）"
    )
    parser.add_argument(
        "--database-url-env",
        default=None,
        help="从该环境变量读目标库 DSN（口令不进 argv；与 --database-url 二选一）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
