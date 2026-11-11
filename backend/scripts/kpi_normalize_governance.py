"""KPI 治理结构化字段规范化 CLI（默认 dry-run）。

派生规则与对账判定在 :mod:`app.services.kpi_governance_normalizer`（纯函数）；
本脚本只负责读库、打印报告、按计划落库。

用法::

    python3 scripts/kpi_normalize_governance.py \
        --database-url 'postgresql+asyncpg://.../qa_metadata'          # dry-run
    python3 scripts/kpi_normalize_governance.py \
        --database-url '...' --apply                                    # 落库

退出码：0 正常；1 目标状态仍有 mismatch / 连错库（拒绝继续）；
2 派生失败或落库复核失败（已回滚，拒绝提交）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

# 直接 `python3 scripts/xxx.py` 运行时 sys.path[0] 是 scripts/ ⇒ `app` / `scripts`
# 都不可导入。必须先把 backend 根插进 sys.path：否则要么 ModuleNotFoundError，
# 要么被 editable 安装的 .pth 引到**主仓**的旧副本（本项目已知的遮蔽陷阱）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.models import (
    KpiCatalog,
    OntologyMetric,
    OntologyMetricSource,
    OntologyProperty,
)
from app.services.kpi_governance_normalizer import (
    MeasurePlan,
    NormalizationError,
    NormalizationPlan,
    deriveNormalization,
    planNormalizations,
    splitTopLevelDivision,
    stripAliases,
    unwrapTrunc,
)
from app.services.kpi_metric_reconcile import VERDICT_MISMATCH, reconcileMetric
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf
from scripts.kpi_metric_db_guard import assertSafeTarget, maskDsn

_EXIT_TARGET_MISMATCH = 1
_EXIT_DERIVATION_ERROR = 2


def _formatList(values: Sequence[str] | None) -> list[str]:
    return list(values or ())


def renderReport(plans: Sequence[NormalizationPlan]) -> str:
    """stdout 报告：逐条 before/after + 汇总行（dry-run 与 apply 共用）。"""
    lines: list[str] = []
    counts = {"pass": 0, "unknown": 0, "mismatch": 0}
    for index, plan in enumerate(plans, start=1):
        counts[plan.verdict_after] = counts.get(plan.verdict_after, 0) + 1
        metricPart = "None" if plan.metric_id is None else str(plan.metric_id)
        noopPart = " noop" if plan.is_noop else ""
        lines.append(
            f"[{index}/{len(plans)}] {plan.kpi_code} (metric={metricPart}) "
            f"before={plan.verdict_before} after={plan.verdict_after} "
            f"tier={plan.tier} basis={plan.basis}{noopPart}"
        )
        lines.append(
            f"    num  {_formatList(plan.before.get('numerator_columns'))!r} -> "
            f"{list(plan.numerator_columns)!r}"
        )
        lines.append(
            f"    den  {_formatList(plan.before.get('denominator_columns'))!r} -> "
            f"{list(plan.denominator_columns)!r}"
        )
        lines.append(
            f"    grain {_formatList(plan.before.get('grain_keys'))!r} -> "
            f"{list(plan.grain_keys)!r}"
        )
        if plan.numerator_measure is not None:
            lines.append(f"    measure.num -> {plan.numerator_measure!r}")
        if plan.denominator_measure is not None:
            lines.append(f"    measure.den -> {plan.denominator_measure!r}")
        if plan.business_todo:
            lines.append(f"    business_todo: {plan.business_todo}")
        for finding in plan.findings_after:
            lines.append(f"    finding {finding}")
    lines.append(
        f"summary: pass={counts.get('pass', 0)} unknown={counts.get('unknown', 0)} "
        f"mismatch={counts.get('mismatch', 0)} (target mismatch=0)"
    )
    return "\n".join(lines)


async def loadSnapshot(
    session: AsyncSession,
) -> tuple[list[KpiCatalog], dict[int, OntologyMetric], dict[int, frozenset[str]]]:
    """一次读全量（禁止逐条查）：KPI / metric / metric→源类物理列。"""
    kpis = list(
        (await session.execute(select(KpiCatalog).order_by(KpiCatalog.kpi_code)))
        .scalars()
        .all()
    )
    metrics = {
        int(metric.id): metric
        for metric in (await session.execute(select(OntologyMetric))).scalars().all()
    }
    rows = (
        await session.execute(
            select(OntologyMetricSource.metric_id, OntologyProperty.source_column)
            .join(
                OntologyProperty,
                OntologyProperty.class_id == OntologyMetricSource.class_id,
            )
            .where(OntologyProperty.source_column.is_not(None))
        )
    ).all()
    byMetric: dict[int, set[str]] = {}
    for metricId, sourceColumn in rows:
        byMetric.setdefault(int(metricId), set()).add(str(sourceColumn))
    return kpis, metrics, {key: frozenset(value) for key, value in byMetric.items()}


def _parseArgs(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KPI 治理结构化字段规范化（默认 dry-run）")
    parser.add_argument("--database-url", required=True, help="目标库 DSN（必填）")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库（默认只打印报告）",
    )
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help=(
            "允许对 qa_metadata 写。⚠️ 守卫只看库名：dev 与 prod 同名，"
            "本开关 + 生产 DSN 会真的写生产 —— 执行前必须自行核对连接串"
        ),
    )
    return parser.parse_args(argv)


async def _main(argv: Sequence[str] | None = None) -> int:
    args = _parseArgs(argv)
    # dev 与 prod 同名（都叫 qa_metadata）：dry-run 只读，允许以它为目标；
    # --apply 必须显式 --allow-prod-write（且调用方先做当日备份）。
    # ⚠️ 守卫**只看库名**，不校验 host/port —— 该开关对生产 DSN 同样放行。
    # 环境区分靠操作者核对下面打印的 host（maskDsn 保留 host:port）。
    targetDb = assertSafeTarget(
        args.database_url,
        allowProdWrite=args.allow_prod_write or not args.apply,
    )

    engine = create_async_engine(args.database_url)
    sessionFactory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionFactory() as session:
            dbNameRow = (await session.execute(text("SELECT current_database()"))).first()
            dbName = "" if dbNameRow is None else str(dbNameRow[0])
            # DSN 里的库名是「声称」，current_database() 是「事实」：两者不一致
            # 说明连到了别的库（连接池/pgbouncer/环境变量串了），一律拒绝继续。
            if dbName != targetDb:
                print(
                    f"[refused] 实际连接库 {dbName!r} 与 DSN 目标 {targetDb!r} 不一致",
                    file=sys.stderr,
                )
                return _EXIT_TARGET_MISMATCH
            print(f"target: {maskDsn(args.database_url)} database={dbName}")
            kpis, metrics, classColumns = await loadSnapshot(session)
            try:
                plans = planNormalizations(kpis, metrics, classColumns)
            except NormalizationError as exc:
                print(f"[refused] {exc}", file=sys.stderr)
                return _EXIT_DERIVATION_ERROR
            print(renderReport(plans))
            if any(plan.verdict_after == "mismatch" for plan in plans):
                print(
                    "[refused] 目标状态仍有 mismatch，拒绝继续（见上方 findings）",
                    file=sys.stderr,
                )
                return _EXIT_TARGET_MISMATCH
            if not args.apply:
                print("dry-run：未写库（加 --apply 落库）")
                return 0
            try:
                changed = await applyPlans(session, plans)
            except NormalizationError as exc:
                print(f"[refused] 落库复核失败（已回滚）：{exc}", file=sys.stderr)
                return _EXIT_DERIVATION_ERROR
            except Exception as exc:  # noqa: BLE001 — DB 级异常不能混进退出码 1
                print(f"[failed] 落库异常（已回滚）：{exc!r}", file=sys.stderr)
                return _EXIT_DERIVATION_ERROR
            print(f"applied：{changed} 条变更已写入 kpi_catalog / ontology_metric")
            return 0
    finally:
        await engine.dispose()


def _sortedColumns(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(sorted(str(v).strip().upper() for v in (values or ()) if str(v).strip()))


async def _verifyApplied(
    session: AsyncSession, plans: Sequence[NormalizationPlan]
) -> None:
    """落库后**读回复核**：值必须等于目标，且重跑对账不得出现 mismatch。

    只信「写成功」不够：JSONB 反序列化、触发器、类型转换都可能改写值。
    """
    session.expire_all()
    codes = [plan.kpi_code for plan in plans]
    rows = (
        await session.execute(
            select(KpiCatalog)
            .where(KpiCatalog.kpi_code.in_(codes))
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    byCode = {row.kpi_code: row for row in rows}
    for plan in plans:
        kpi = byCode.get(plan.kpi_code)
        if kpi is None:
            raise NormalizationError(f"{plan.kpi_code}: 落库后读不到 KPI 行")
        # 三组列都按**无序**比较：治理列序不参与任何判定，plan 的 grain 保持
        # 作者原序（``_tokens`` 不排序），拿排序结果去比未排序目标会误报。
        if _sortedColumns(kpi.numerator_columns) != _sortedColumns(plan.numerator_columns):
            raise NormalizationError(f"{plan.kpi_code}: 分子列落库值与目标不一致")
        if _sortedColumns(kpi.denominator_columns) != _sortedColumns(
            plan.denominator_columns
        ):
            raise NormalizationError(f"{plan.kpi_code}: 分母列落库值与目标不一致")
        if _sortedColumns(kpi.grain_keys) != _sortedColumns(plan.grain_keys):
            raise NormalizationError(f"{plan.kpi_code}: 粒度键落库值与目标不一致")
        if plan.metric_id is not None and kpi.metric_id != plan.metric_id:
            raise NormalizationError(
                f"{plan.kpi_code}: metric_id 已变化（库 {kpi.metric_id} ≠ 计划 "
                f"{plan.metric_id}），落库后复核不通过"
            )
        metric = (
            await session.get(
                OntologyMetric, plan.metric_id, populate_existing=True
            )
            if plan.metric_id is not None
            else None
        )
        if metric is None:
            raise NormalizationError(f"{plan.kpi_code}: 落库后读不到 ontology_metric")
        for field, expected in (
            ("numerator_measure", plan.numerator_measure),
            ("denominator_measure", plan.denominator_measure),
        ):
            if expected is not None and getattr(metric, field) != expected:
                raise NormalizationError(f"{plan.kpi_code}: {field} 落库值与目标不一致")
        result = reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))
        if result.verdict == VERDICT_MISMATCH:
            raise NormalizationError(
                f"{plan.kpi_code}: 落库后对账仍 mismatch：{list(result.reasons())}"
            )
        # 不止「不是 mismatch」：pass 计划回落成 unknown 同样是失败（Tier 降级）。
        if plan.verdict_after and result.verdict != plan.verdict_after:
            raise NormalizationError(
                f"{plan.kpi_code}: 落库后 verdict={result.verdict}，"
                f"与计划 {plan.verdict_after} 不一致"
            )


def _assertUniqueTargets(plans: Sequence[NormalizationPlan]) -> None:
    """同一 kpi_code / metric_id 只能出现一次：后写会静默覆盖先写。"""
    codes: dict[str, int] = {}
    metricIds: dict[int, str] = {}
    for plan in plans:
        if plan.kpi_code in codes:
            raise NormalizationError(f"{plan.kpi_code}: 计划里出现重复 kpi_code")
        codes[plan.kpi_code] = 1
        if plan.metric_id is None:
            continue
        if plan.metric_id in metricIds:
            raise NormalizationError(
                f"metric_id {plan.metric_id} 被多个 KPI 计划共享"
                f"（{metricIds[plan.metric_id]} / {plan.kpi_code}）"
            )
        metricIds[plan.metric_id] = plan.kpi_code


async def _assertOwnership(
    session: AsyncSession, plans: Sequence[NormalizationPlan]
) -> None:
    """落库前**重读**：计划依赖的字段必须仍是快照时的值。

    三类并发改动都要拦：

    1. ``metric_id`` 挂靠变了 —— 否则度量会写到别的指标上，且复核用自己的
       metric 永远自洽；
    2. 治理列被改了 —— 直接覆盖会静默丢掉别人的编辑；
    3. 度量被改了 —— 计划里的治理列是从旧度量推导的，写下去就是过期值。

    只查 ``metric_id`` 不够：2/3 两类的写入本身「成功」，错误要等到复核才暴露，
    而那时代码已经改过库里的值（虽然会回滚，但拒绝写库才是正确语义）。
    """
    codes = [plan.kpi_code for plan in plans]
    rows = (
        await session.execute(
            select(KpiCatalog)
            .where(KpiCatalog.kpi_code.in_(codes))
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    byCode = {row.kpi_code: row for row in rows}
    metricIds = [plan.metric_id for plan in plans if plan.metric_id is not None]
    metrics: dict[int, OntologyMetric] = {}
    if metricIds:
        metricRows = (
            await session.execute(
                select(OntologyMetric)
                .where(OntologyMetric.id.in_(metricIds))
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        metrics = {int(metric.id): metric for metric in metricRows}
    for plan in plans:
        kpi = byCode.get(plan.kpi_code)
        if kpi is None:
            raise NormalizationError(f"{plan.kpi_code}: 库里找不到该 KPI，拒绝写库")
        if plan.metric_id is not None and kpi.metric_id != plan.metric_id:
            raise NormalizationError(
                f"{plan.kpi_code}: metric_id 已变化（库 {kpi.metric_id} ≠ 计划 "
                f"{plan.metric_id}），拒绝写库"
            )
        for field in ("numerator_columns", "denominator_columns", "grain_keys"):
            if list(getattr(kpi, field) or ()) != list(plan.before.get(field) or ()):
                raise NormalizationError(
                    f"{plan.kpi_code}: {field} 在计划快照后已被改动，拒绝写库"
                )
        if plan.metric_id is None:
            continue
        metric = metrics.get(plan.metric_id)
        if metric is None:
            raise NormalizationError(
                f"{plan.kpi_code}: 库里找不到 metric {plan.metric_id}，拒绝写库"
            )
        for field in ("numerator_measure", "denominator_measure"):
            if getattr(metric, field) != plan.before.get(field):
                raise NormalizationError(
                    f"{plan.kpi_code}: {field} 在计划快照后已被改动，拒绝写库"
                )


async def applyPlans(
    session: AsyncSession, plans: Sequence[NormalizationPlan]
) -> int:
    """按计划落库并复核；返回实际变更的 KPI 条数。

    单事务：任一条失败（更新行数 ≠ 1 / 复核不一致 / 仍 mismatch）⇒ 全部回滚。
    ``is_noop`` 的条目跳过，所以重跑不会产生无意义写入。
    """
    mismatched = [plan.kpi_code for plan in plans if plan.verdict_after == "mismatch"]
    if mismatched:
        raise NormalizationError(
            f"目标状态仍有 mismatch，拒绝写库：{', '.join(mismatched)}"
        )
    _assertUniqueTargets(plans)
    pending = [plan for plan in plans if not plan.is_noop]
    if not pending:
        return 0
    try:
        await _assertOwnership(session, pending)
        for plan in pending:
            result = await session.execute(
                update(KpiCatalog)
                .where(KpiCatalog.kpi_code == plan.kpi_code)
                .values(
                    numerator_columns=list(plan.numerator_columns),
                    denominator_columns=list(plan.denominator_columns),
                    grain_keys=list(plan.grain_keys),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise NormalizationError(
                    f"{plan.kpi_code}: 更新行数 {result.rowcount} ≠ 1，拒绝继续"
                )
            if plan.numerator_measure is None and plan.denominator_measure is None:
                continue
            measureValues: dict[str, object] = {}
            if plan.numerator_measure is not None:
                measureValues["numerator_measure"] = plan.numerator_measure
            if plan.denominator_measure is not None:
                measureValues["denominator_measure"] = plan.denominator_measure
            if plan.metric_id is None:
                raise NormalizationError(f"{plan.kpi_code}: 缺 metric_id，无法写度量")
            result = await session.execute(
                update(OntologyMetric)
                .where(OntologyMetric.id == plan.metric_id)
                .values(**measureValues)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise NormalizationError(
                    f"{plan.kpi_code}: metric 更新行数 {result.rowcount} ≠ 1，拒绝继续"
                )
        await _verifyApplied(session, pending)
        await session.commit()
    except Exception:
        # 不止 NormalizationError：DB 级异常同样必须回滚，否则调用方（本函数
        # 对外导出、测试直接调用）在同一 session 上后续 commit 会带出半截写入。
        await session.rollback()
        raise
    return len(pending)


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


__all__ = [
    "MeasurePlan",
    "NormalizationError",
    "NormalizationPlan",
    "applyPlans",
    "deriveNormalization",
    "loadSnapshot",
    "main",
    "planNormalizations",
    "renderReport",
    "splitTopLevelDivision",
    "stripAliases",
    "unwrapTrunc",
]


if __name__ == "__main__":
    raise SystemExit(main())
