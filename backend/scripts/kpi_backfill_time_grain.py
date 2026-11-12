"""KPI ``time_grain`` / ``time_column`` 幂等回填 CLI（默认 dry-run）。

派生规则（R3/R6）在 :mod:`app.services.kpi_time_grain_backfill`（纯函数）；
本脚本只负责读库、打印报告、按计划落库（骨架同
:mod:`scripts.kpi_normalize_governance`）。

用法::

    python3 scripts/kpi_backfill_time_grain.py \
        --database-url 'postgresql+asyncpg://.../qa_metadata'          # dry-run
    python3 scripts/kpi_backfill_time_grain.py \
        --database-url '...' --apply                                    # 落库
    python3 scripts/kpi_backfill_time_grain.py \
        --database-url '...' --apply --allow-prod-write                 # 写正式库（先备份）

退出码：
0 正常（dry-run 或 apply 成功）；1 拒绝继续（连错库 / 0143 未迁移时 apply）；
2 落库失败或复核不一致（已回滚）。

⚠️ 0143 三列不存在（未迁移库）时：dry-run 仍输出计划报告并标注
``[schema-not-migrated]``；``--apply`` 一律拒绝（脚本不做 DDL）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

# 直接 `python3 scripts/xxx.py` 运行时 sys.path[0] 是 scripts/ ⇒ `app` / `scripts`
# 都不可导入。必须先把 backend 根插进 sys.path（见 kpi_normalize_governance.py
# 同款注释：editable 安装的旧副本遮蔽陷阱）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.domain.models import KpiCatalog, OntologyMetric  # noqa: E402
from app.services.kpi_alignment_resolver import (  # noqa: E402
    SourceProperty,
    sourcePropertiesForMetric,
)
from app.services.kpi_time_grain_backfill import (  # noqa: E402
    BackfillPlan,
    KpiSnapshot,
    MetricSnapshot,
    SchemaAvailability,
    planBackfills,
    renderReport,
)
from scripts.kpi_metric_db_guard import assertSafeTarget, maskDsn  # noqa: E402

_EXIT_REFUSED = 1
_EXIT_APPLY_FAILED = 2


class BackfillError(Exception):
    """落库前校验 / 落库后复核失败（触发整体回滚）。"""


async def detectSchemaAvailability(session: AsyncSession) -> SchemaAvailability:
    """0143 三列存在性（只读 information_schema，不做任何 DDL）。"""
    rows = (
        await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name IN ('kpi_catalog', 'ontology_metric') "
                "AND column_name IN ('time_grain', 'time_column')"
            )
        )
    ).all()
    present = {(str(table), str(column)) for table, column in rows}
    return SchemaAvailability(
        kpiTimeGrain=("kpi_catalog", "time_grain") in present,
        metricTimeGrain=("ontology_metric", "time_grain") in present,
        metricTimeColumn=("ontology_metric", "time_column") in present,
    )


async def loadSnapshot(
    session: AsyncSession, availability: SchemaAvailability
) -> tuple[
    list[KpiSnapshot],
    dict[int, MetricSnapshot],
    dict[int, tuple[SourceProperty, ...]],
]:
    """一次读 KPI / metric 全量 + 每个挂靠 metric 的源类属性（只读）。

    未迁移库缺列时**不选**新列：SQLAlchemy 只 SELECT 显式列表达式，
    不会因 ORM 声明了缺列而整条查询失败。
    """
    kpiColumns = [
        KpiCatalog.id,
        KpiCatalog.kpi_code,
        KpiCatalog.grain,
        KpiCatalog.grain_keys,
        KpiCatalog.metric_id,
    ]
    if availability.kpiTimeGrain:
        kpiColumns.append(KpiCatalog.time_grain)
    kpiRows = (
        await session.execute(select(*kpiColumns).order_by(KpiCatalog.kpi_code))
    ).all()
    kpis = [
        KpiSnapshot(
            kpi_id=int(row.id),
            kpi_code=str(row.kpi_code),
            grain=row.grain,
            grain_keys=tuple(row.grain_keys or ()),
            metric_id=row.metric_id,
            time_grain=(
                getattr(row, "time_grain", None) if availability.kpiTimeGrain else None
            ),
        )
        for row in kpiRows
    ]

    metricColumns = [
        OntologyMetric.id,
        OntologyMetric.formula,
        OntologyMetric.numerator_measure,
        OntologyMetric.denominator_measure,
    ]
    if availability.metricTimeGrain:
        metricColumns.append(OntologyMetric.time_grain)
    if availability.metricTimeColumn:
        metricColumns.append(OntologyMetric.time_column)
    metricRows = (await session.execute(select(*metricColumns))).all()
    metrics = {
        int(row.id): MetricSnapshot(
            metric_id=int(row.id),
            formula=row.formula,
            numerator_measure=row.numerator_measure,
            denominator_measure=row.denominator_measure,
            time_grain=(
                getattr(row, "time_grain", None) if availability.metricTimeGrain else None
            ),
            time_column=(
                getattr(row, "time_column", None) if availability.metricTimeColumn else None
            ),
        )
        for row in metricRows
    }

    propertiesByMetric: dict[int, tuple[SourceProperty, ...]] = {}
    for metricId in metrics:
        propertiesByMetric[metricId] = await sourcePropertiesForMetric(session, metricId)
    return kpis, metrics, propertiesByMetric


def _normalizeGrain(value: str | None) -> str | None:
    return None if value is None else value.strip().lower()


def _normalizeColumn(value: str | None) -> str | None:
    return None if value is None else value.strip().upper()


async def _readTimeFields(
    session: AsyncSession, kpiIds: Sequence[int], metricIds: Sequence[int]
) -> tuple[dict[int, object], dict[int, object]]:
    """按 id 重读回填依赖的列（列表达式 select，绕开身份映射缓存）。"""
    kpiRows = (
        await session.execute(
            select(KpiCatalog.id, KpiCatalog.metric_id, KpiCatalog.time_grain).where(
                KpiCatalog.id.in_(kpiIds)
            )
        )
    ).all()
    metricRows: dict[int, object] = {}
    if metricIds:
        rows = (
            await session.execute(
                select(
                    OntologyMetric.id,
                    OntologyMetric.time_grain,
                    OntologyMetric.time_column,
                ).where(OntologyMetric.id.in_(metricIds))
            )
        ).all()
        metricRows = {int(row.id): row for row in rows}
    return {int(row.id): row for row in kpiRows}, metricRows


async def _assertSnapshotIsCurrent(
    session: AsyncSession, plans: Sequence[BackfillPlan], availability: SchemaAvailability
) -> None:
    """落库前重读：计划依赖的字段必须仍是快照时的值（防并发改动被覆盖）。"""
    metricIds = [plan.metric_id for plan in plans if plan.metric_id is not None]
    byId, metricRows = await _readTimeFields(
        session, [plan.kpi_id for plan in plans], metricIds
    )
    for plan in plans:
        row = byId.get(plan.kpi_id)
        if row is None:
            raise BackfillError(f"{plan.kpi_code}: 库中找不到该 KPI，拒绝写库")
        if row.metric_id != plan.metric_id:
            raise BackfillError(
                f"{plan.kpi_code}: metric_id 已变化（库 {row.metric_id} ≠ 计划 "
                f"{plan.metric_id}），拒绝写库"
            )
        if availability.kpiTimeGrain and _normalizeGrain(
            getattr(row, "time_grain", None)
        ) != _normalizeGrain(plan.before_kpi_time_grain):
            raise BackfillError(
                f"{plan.kpi_code}: kpi_catalog.time_grain 在快照后已被改动，拒绝写库"
            )
        if plan.metric_id is None:
            continue
        metricRow = metricRows.get(plan.metric_id)
        if metricRow is None:
            raise BackfillError(
                f"{plan.kpi_code}: 库中找不到 metric {plan.metric_id}，拒绝写库"
            )
        if availability.metricTimeGrain and _normalizeGrain(
            getattr(metricRow, "time_grain", None)
        ) != _normalizeGrain(plan.before_metric_time_grain):
            raise BackfillError(
                f"{plan.kpi_code}: ontology_metric.time_grain 在快照后已被改动，"
                "拒绝写库"
            )
        if availability.metricTimeColumn and _normalizeColumn(
            getattr(metricRow, "time_column", None)
        ) != _normalizeColumn(plan.before_metric_time_column):
            raise BackfillError(
                f"{plan.kpi_code}: ontology_metric.time_column 在快照后已被改动，"
                "拒绝写库"
            )


async def _verifyApplied(
    session: AsyncSession, plans: Sequence[BackfillPlan]
) -> None:
    """落库后读回复核：目标值必须等于计划值（不只信 rowcount）。"""
    metricIds = [plan.metric_id for plan in plans if plan.metric_id is not None]
    kpiById, metricRows = await _readTimeFields(
        session, [plan.kpi_id for plan in plans], metricIds
    )
    for plan in plans:
        kpiRow = kpiById.get(plan.kpi_id)
        if kpiRow is None:
            raise BackfillError(f"{plan.kpi_code}: 落库后读不到 KPI 行")
        if plan.has_kpi_grain_change and _normalizeGrain(
            getattr(kpiRow, "time_grain", None)
        ) != _normalizeGrain(plan.target_kpi_time_grain):
            raise BackfillError(
                f"{plan.kpi_code}: kpi_catalog.time_grain 落库值与目标不一致"
            )
        if plan.metric_id is None:
            continue
        metricRow = metricRows.get(plan.metric_id)
        if metricRow is None:
            raise BackfillError(f"{plan.kpi_code}: 落库后读不到 ontology_metric")
        if plan.has_metric_grain_change and _normalizeGrain(
            getattr(metricRow, "time_grain", None)
        ) != _normalizeGrain(plan.target_metric_time_grain):
            raise BackfillError(
                f"{plan.kpi_code}: ontology_metric.time_grain 落库值与目标不一致"
            )
        if plan.has_metric_column_change and _normalizeColumn(
            getattr(metricRow, "time_column", None)
        ) != _normalizeColumn(plan.target_metric_time_column):
            raise BackfillError(
                f"{plan.kpi_code}: ontology_metric.time_column 落库值与目标不一致"
            )


async def applyPlans(
    session: AsyncSession,
    plans: Sequence[BackfillPlan],
    availability: SchemaAvailability,
) -> int:
    """按计划落库并复核；返回实际变更的 KPI 条数。

    单事务：任一条失败（rowcount ≠ 1 / 复核不一致）⇒ 全部回滚；
    无变更条目跳过 ⇒ 重跑幂等返回 0。
    """
    if not availability.isComplete:
        raise BackfillError("0143 列未迁移，拒绝写库")
    pending = [plan for plan in plans if plan.has_changes]
    if not pending:
        return 0
    try:
        await _assertSnapshotIsCurrent(session, pending, availability)
        for plan in pending:
            if plan.has_kpi_grain_change:
                result = await session.execute(
                    update(KpiCatalog)
                    .where(KpiCatalog.id == plan.kpi_id)
                    .values(time_grain=plan.target_kpi_time_grain)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise BackfillError(
                        f"{plan.kpi_code}: kpi 更新行数 {result.rowcount} ≠ 1，拒绝继续"
                    )
            if plan.metric_id is None:
                continue
            values: dict[str, object] = {}
            if plan.has_metric_grain_change:
                values["time_grain"] = plan.target_metric_time_grain
            if plan.has_metric_column_change:
                values["time_column"] = plan.target_metric_time_column
            if not values:
                continue
            result = await session.execute(
                update(OntologyMetric)
                .where(OntologyMetric.id == plan.metric_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise BackfillError(
                    f"{plan.kpi_code}: metric 更新行数 {result.rowcount} ≠ 1，拒绝继续"
                )
        await _verifyApplied(session, pending)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return len(pending)


# ── CLI ──────────────────────────────────────────────────────────────


def _parseArgs(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KPI time_grain / time_column 幂等回填（默认 dry-run）"
    )
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
    # dry-run 只读，允许以 qa_metadata 为目标；--apply 必须显式 --allow-prod-write。
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
            # DSN 是「声称」，current_database() 是「事实」：不一致 = 连错库。
            if dbName != targetDb:
                print(
                    f"[refused] 实际连接库 {dbName!r} 与 DSN 目标 {targetDb!r} 不一致",
                    file=sys.stderr,
                )
                return _EXIT_REFUSED
            print(f"target: {maskDsn(args.database_url)} database={dbName}")

            availability = await detectSchemaAvailability(session)
            if not availability.isComplete:
                message = (
                    "[schema-not-migrated] 0143 列缺失："
                    + ", ".join(availability.missing())
                )
                if args.apply:
                    print(
                        f"[refused] {message}；apply 拒绝（脚本不做 DDL）",
                        file=sys.stderr,
                    )
                    return _EXIT_REFUSED
                print(f"{message}（dry-run 继续：以下为 planned 值，不可写）")

            kpis, metrics, propertiesByMetric = await loadSnapshot(session, availability)
            plans = planBackfills(kpis, metrics, propertiesByMetric)
            print(renderReport(plans, availability))

            if not args.apply:
                print("dry-run：未写库（加 --apply 落库）")
                return 0
            try:
                changed = await applyPlans(session, plans, availability)
            except BackfillError as exc:
                print(f"[refused] 落库复核失败（已回滚）：{exc}", file=sys.stderr)
                return _EXIT_APPLY_FAILED
            except Exception as exc:  # noqa: BLE001 — DB 级异常不能混进退出码 1
                print(f"[failed] 落库异常（已回滚）：{exc!r}", file=sys.stderr)
                return _EXIT_APPLY_FAILED
            print(
                f"applied：{changed} 条变更已写入 "
                "kpi_catalog.time_grain / ontology_metric.time_grain|time_column"
            )
            return 0
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


__all__ = [
    "BackfillError",
    "applyPlans",
    "detectSchemaAvailability",
    "loadSnapshot",
    "main",
    "planBackfills",
    "renderReport",
]


if __name__ == "__main__":
    raise SystemExit(main())
