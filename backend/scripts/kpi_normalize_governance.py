"""KPI 治理结构化字段规范化 CLI（默认 dry-run）。

派生规则与对账判定在 :mod:`app.services.kpi_governance_normalizer`（纯函数）；
本脚本只负责读库、打印报告、按计划落库。

用法::

    python3 scripts/kpi_normalize_governance.py \
        --database-url 'postgresql+asyncpg://.../qa_metadata'          # dry-run
    python3 scripts/kpi_normalize_governance.py \
        --database-url '...' --apply                                    # 落库

退出码：0 正常；1 目标状态仍有 mismatch（拒绝继续）；2 派生失败（拒绝写库）。
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

from sqlalchemy import select, text  # noqa: E402
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
        help="允许对 qa_metadata 写（仅 dev 5433；生产永远拒绝）",
    )
    return parser.parse_args(argv)


async def _main(argv: Sequence[str] | None = None) -> int:
    args = _parseArgs(argv)
    # dev 与 prod 同名（都叫 qa_metadata）：dry-run 只读，允许以它为目标；
    # --apply 必须显式 --allow-prod-write（且调用方先做当日备份）。
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
                await applyPlans(session, plans)
            except NormalizationError as exc:
                print(f"[refused] 落库前复核失败：{exc}", file=sys.stderr)
                return _EXIT_DERIVATION_ERROR
            print("applied：已写入 kpi_catalog / ontology_metric")
            return 0
    finally:
        await engine.dispose()


async def applyPlans(
    session: AsyncSession, plans: Sequence[NormalizationPlan]
) -> None:
    """按计划落库（D3 实现）；调用方须先确认目标无 mismatch。"""
    raise NotImplementedError("D3 实现")


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
