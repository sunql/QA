"""采购域 ``property_alias`` 回填 CLI（默认 dry-run）。

规划 / 校验是纯函数（:mod:`app.services.kpi_property_alias_backfill`）；
本脚本只负责读库、**模拟对账**、按计划落库。

为什么要「模拟对账」
--------------------
alias 只影响 resolver 的阶段 3。若某个治理 token 目前落在 missing，
回填后可能变成 unknown（多候选）或 resolved —— 那会改变层 2 的结论。
本脚本在写库前把**有源类的 KPI**（当前 9 条；3 条无源类不具备模拟条件）
的对账**前后各算一遍**，结论不一致即拒绝写库。

用法::

    python3 scripts/backfill_property_alias.py --database-url '...'          # dry-run
    python3 scripts/backfill_property_alias.py --database-url '...' --apply   # 落库
    python3 scripts/backfill_property_alias.py --database-url '...' --apply --allow-prod-write

退出码：0 正常 / 1 拒绝（守卫、冲突、模拟不一致）/ 2 落库复核失败（已回滚）。

⚠️ 回填后 **live alias ≠ 已发布快照（``semantic_release.manifest_json``）**，
``ENABLE_SEMANTIC_RELEASE`` 两侧会分叉 ⇒ 本批只出 mapping + dry-run 证据，
``--apply`` 与 A9「采购域首次发布」同批执行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.domain.models import (  # noqa: E402
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
    OntologyProperty,
)
from app.services.kpi_alignment_resolver import (  # noqa: E402
    SourceProperty,
    resolveGovernanceTokens,
)
from app.services.kpi_metric_reconcile import governanceTokensOf, reconcileMetric  # noqa: E402
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf  # noqa: E402
from app.services.kpi_property_alias_backfill import (  # noqa: E402
    AliasBackfillError,
    AliasEntry,
    AliasPlan,
    ExcludedTarget,
    PropertyRow,
    assertMappingCoverage,
    findScopeCollisions,
    parseExcluded,
    parseMapping,
    planAliasFill,
    renderReport,
)
from scripts.db_snapshot_guard import ensureBackups  # noqa: E402
from scripts.kpi_metric_attribution_probe import MetricRefs, referenceTokens  # noqa: E402
from scripts.kpi_metric_db_guard import PROD_DB_NAME, assertSafeTarget, maskDsn  # noqa: E402

_EXIT_REFUSED = 1
_EXIT_APPLY_FAILED = 2

DEFAULT_MAPPING = (
    Path(__file__).resolve().parents[2] / "docs/update/1118/M0/property-alias-mapping.json"
)

# 闸 4（prod 写库前备份）：只备份本批会改的表。
_BACKUP_DIR = Path(tempfile.gettempdir()) / "kpi_property_alias_backups"
_BACKUP_TABLES = ("ontology_property",)


# ── 读库 ──────────────────────────────────────────────────────────────


async def _loadProperties(session: AsyncSession, classNames: set[str]) -> tuple[PropertyRow, ...]:
    """读目标类的属性行。

    仅排除「墓碑且**没有任何指标在引用**」的类：库里确有被软删（``valid_to``
    非空）却仍被 ``ontology_metric_source`` 绑定的类 —— 直接按 ``valid_to``
    过滤会把它们的目标全部判成「不存在」。类重名/多版本由
    ``planAliasFill`` 的歧义检查兜底（按 ``class_id`` 定位，不猜）。
    """
    sourceClassIds = (
        select(OntologyMetricSource.class_id).distinct().scalar_subquery()
    )
    rows = (
        await session.execute(
            select(
                OntologyClass.id,
                OntologyClass.class_name,
                OntologyProperty.property_name,
                OntologyProperty.source_column,
                OntologyProperty.property_alias,
            ).join(OntologyProperty, OntologyProperty.class_id == OntologyClass.id)
            .where(OntologyClass.class_name.in_(sorted(classNames)))
            .where(
                (OntologyClass.valid_to.is_(None))
                | (OntologyClass.id.in_(sourceClassIds))
            )
        )
    ).all()
    return tuple(
        PropertyRow(
            class_name=str(row.class_name),
            property_name=str(row.property_name),
            source_column=row.source_column,
            current_alias=row.property_alias,
            class_id=int(row.id),
        )
        for row in rows
    )


async def _loadExistingAliases(session: AsyncSession) -> dict[tuple[str, str], str]:
    """库内**已有** alias —— 冲突判定必须把存量算进去（与运行时同取全量，不按 valid_to 过滤）。"""
    rows = (
        await session.execute(
            select(
                OntologyClass.class_name,
                OntologyProperty.source_column,
                OntologyProperty.property_alias,
            )
            .join(OntologyProperty, OntologyProperty.class_id == OntologyClass.id)
            .where(OntologyProperty.source_column.isnot(None))
            .where(
                (OntologyProperty.property_alias.isnot(None))
                & (text("btrim(ontology_property.property_alias) <> ''"))
            )
        )
    ).all()
    return {
        (str(row.class_name).strip().upper(), str(row.source_column).strip().upper()): str(
            row.property_alias
        ).strip()
        for row in rows
    }


async def _loadScopes(session: AsyncSession) -> dict[str, set[tuple[str, str]]]:
    """``{kpi_code: {(CLASS, COLUMN)}}`` —— 用于作用域内 alias 歧义检查。"""
    rows = (
        await session.execute(
            select(KpiCatalog.kpi_code, OntologyClass.class_name, OntologyProperty.source_column)
            .join(OntologyMetricSource, OntologyMetricSource.metric_id == KpiCatalog.metric_id)
            .join(OntologyClass, OntologyClass.id == OntologyMetricSource.class_id)
            .join(OntologyProperty, OntologyProperty.class_id == OntologyClass.id)
            .where(OntologyProperty.source_column.isnot(None))
        )
    ).all()
    scopes: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        scopes.setdefault(str(row.kpi_code), set()).add(
            (str(row.class_name), str(row.source_column))
        )
    return scopes


async def _loadPropertiesByMetric(
    session: AsyncSession,
) -> dict[int, tuple[tuple[str, SourceProperty], ...]]:
    rows = (
        await session.execute(
            select(
                OntologyMetricSource.metric_id,
                OntologyClass.class_name,
                OntologyProperty.property_name,
                OntologyProperty.source_column,
                OntologyProperty.property_alias,
                OntologyProperty.business_aliases,
                OntologyProperty.data_type,
            )
            .join(OntologyClass, OntologyClass.id == OntologyMetricSource.class_id)
            .join(OntologyProperty, OntologyProperty.class_id == OntologyClass.id)
        )
    ).all()
    result: dict[int, list[tuple[str, SourceProperty]]] = {}
    for row in rows:
        result.setdefault(int(row.metric_id), []).append(
            (
                str(row.class_name),
                SourceProperty(
                    property_name=str(row.property_name),
                    source_column=row.source_column,
                    property_alias=row.property_alias,
                    business_aliases=tuple(row.business_aliases or ()),
                    data_type=row.data_type,
                ),
            )
        )
    return {metricId: tuple(items) for metricId, items in result.items()}


async def _loadReferencedTargets(session: AsyncSession) -> set[tuple[str, str]]:
    """所有 KPI 技术形态引用的 (源类, 物理列) —— 映射覆盖率校验的基准。

    列名只有「列」，落到哪个源类要把每个 KPI 的源类逐个展开；
    同一列出现在两个源类（join 键副本）⇒ 两个 (类, 列) 都要被映射覆盖。
    SQL 不可解析的 KPI（``referenceTokens is None``）不参与：它们没有可靠引用。
    """
    metrics = {
        int(row.id): row for row in (await session.execute(select(OntologyMetric))).scalars().all()
    }
    rows = (
        await session.execute(
            select(
                OntologyMetricSource.metric_id,
                OntologyClass.class_name,
                OntologyProperty.source_column,
            )
            .join(OntologyClass, OntologyClass.id == OntologyMetricSource.class_id)
            .join(OntologyProperty, OntologyProperty.class_id == OntologyClass.id)
            .where(OntologyProperty.source_column.isnot(None))
        )
    ).all()
    columnsByMetric: dict[int, set[tuple[str, str]]] = {}
    for row in rows:
        columnsByMetric.setdefault(int(row.metric_id), set()).add(
            (str(row.class_name), str(row.source_column))
        )

    targets: set[tuple[str, str]] = set()
    kpis = (await session.execute(select(KpiCatalog))).scalars().all()
    for kpi in kpis:
        metric = metrics.get(int(kpi.metric_id)) if kpi.metric_id is not None else None
        if metric is None:
            continue
        tokens = referenceTokens(
            MetricRefs(
                numerator_measure=metric.numerator_measure,
                denominator_measure=metric.denominator_measure,
                formula=metric.formula,
                filter_expression=metric.filter_expression,
                grain_keys=tuple(metric.grain_keys or ()),
                time_column=metric.time_column,
            )
        )
        if tokens is None:
            continue
        for className, column in columnsByMetric.get(int(metric.id), set()):
            if column.strip().upper() in tokens:
                targets.add((className, column))
    return targets


# ── 模拟对账 ──────────────────────────────────────────────────────────


def _aliasTarget(plans: Sequence[AliasPlan]) -> dict[tuple[str, str], str]:
    return {
        (plan.class_name.upper(), plan.source_column.upper()): plan.alias
        for plan in plans
        if plan.has_change
    }


def _applyAliases(
    items: tuple[tuple[str, SourceProperty], ...], target: dict[tuple[str, str], str]
) -> tuple[SourceProperty, ...]:
    """模拟回填后的属性视图：只填空 alias，不覆盖（与落库语义一致）。"""
    result: list[SourceProperty] = []
    for className, prop in items:
        alias = None
        if prop.source_column is not None and not (prop.property_alias or "").strip():
            alias = target.get((className.upper(), prop.source_column.upper()))
        result.append(
            SourceProperty(
                property_name=prop.property_name,
                source_column=prop.source_column,
                property_alias=alias or prop.property_alias,
                business_aliases=prop.business_aliases,
                data_type=prop.data_type,
            )
        )
    return tuple(result)


def _simulateOne(
    kpi: KpiCatalog,
    metric: OntologyMetric | None,
    items: tuple[tuple[str, SourceProperty], ...],
    target: dict[tuple[str, str], str],
) -> dict[str, object]:
    governance = governanceSpecOf(kpi)
    technique = techniqueOf(metric)
    tokens = governanceTokensOf(governance)
    before = resolveGovernanceTokens(tokens, tuple(prop for _, prop in items))
    after = resolveGovernanceTokens(tokens, _applyAliases(items, target))
    verdictBefore = reconcileMetric(governance, technique, before).verdict
    verdictAfter = reconcileMetric(governance, technique, after).verdict
    return {
        "kpiCode": str(kpi.kpi_code),
        "verdictBefore": verdictBefore,
        "verdictAfter": verdictAfter,
        "resolvedChanged": tuple(before.resolved) != tuple(after.resolved),
        "unknownBefore": len(before.unknownTokens),
        "unknownAfter": len(after.unknownTokens),
        "missingBefore": len(before.missingTokens),
        "missingAfter": len(after.missingTokens),
    }


async def simulateVerdicts(
    session: AsyncSession, plans: Sequence[AliasPlan]
) -> tuple[dict[str, object], ...]:
    target = _aliasTarget(plans)
    propertiesByMetric = await _loadPropertiesByMetric(session)
    kpis = (await session.execute(select(KpiCatalog).order_by(KpiCatalog.kpi_code))).scalars().all()
    metrics = {
        int(row.id): row for row in (await session.execute(select(OntologyMetric))).scalars().all()
    }
    rows: list[dict[str, object]] = []
    for kpi in kpis:
        if kpi.metric_id is None:
            continue
        items = propertiesByMetric.get(int(kpi.metric_id), ())
        if not items:
            continue
        rows.append(_simulateOne(kpi, metrics.get(int(kpi.metric_id)), items, target))
    return tuple(rows)


def _renderSimulation(rows: Sequence[dict[str, object]]) -> tuple[str, int]:
    lines = ["", "模拟对账（回填前 → 回填后）"]
    changed = 0
    for row in rows:
        same = (
            row["verdictBefore"] == row["verdictAfter"]
            and not row["resolvedChanged"]
            and row["unknownBefore"] == row["unknownAfter"]
            and row["missingBefore"] == row["missingAfter"]
        )
        if not same:
            changed += 1
        lines.append(
            f"  {row['kpiCode']}: verdict {row['verdictBefore']} → {row['verdictAfter']}；"
            f"unknown {row['unknownBefore']}→{row['unknownAfter']}；"
            f"missing {row['missingBefore']}→{row['missingAfter']}"
            + ("" if same else "  ← 不一致")
        )
    lines.append(
        f"结论：{len(rows)} 条参与模拟，解析/对账变化 {changed} 条"
        + ("" if changed == 0 else "（拒绝写库）")
    )
    return "\n".join(lines), changed


# ── 落库 ──────────────────────────────────────────────────────────────


async def applyPlans(session: AsyncSession, plans: Sequence[AliasPlan]) -> int:
    """只写 ``property_alias`` 仍为空的行；单事务，任一行不符即整体回滚。"""
    pending = [plan for plan in plans if plan.has_change]
    if not pending:
        return 0
    try:
        for plan in pending:
            # 定位键 = class_id + 精确列名：class_name 不唯一（多版本 / 墓碑行）。
            result = await session.execute(
                update(OntologyProperty)
                .where(OntologyProperty.class_id == plan.class_id)
                .where(OntologyProperty.source_column == plan.source_column)
                .where(
                    (OntologyProperty.property_alias.is_(None))
                    | (text("btrim(ontology_property.property_alias) = ''"))
                )
                .values(property_alias=plan.alias)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise AliasBackfillError(
                    f"{plan.class_name}(class_id={plan.class_id}).{plan.source_column}: "
                    f"更新行数 {result.rowcount} ≠ 1（目标已被改动或列不唯一），拒绝继续"
                )
        await _verifyApplied(session, pending)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return len(pending)


async def _verifyApplied(session: AsyncSession, plans: Sequence[AliasPlan]) -> None:
    for plan in plans:
        value = (
            await session.execute(
                select(OntologyProperty.property_alias)
                .where(OntologyProperty.class_id == plan.class_id)
                .where(OntologyProperty.source_column == plan.source_column)
            )
        ).scalar_one_or_none()
        if value != plan.alias:
            raise AliasBackfillError(
                f"{plan.class_name}(class_id={plan.class_id}).{plan.source_column}: "
                f"落库值 {value!r} ≠ 目标 {plan.alias!r}"
            )


# ── CLI ──────────────────────────────────────────────────────────────


def _parseArgs(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采购域 property_alias 回填（默认 dry-run）")
    parser.add_argument("--database-url", required=True, help="目标库 DSN（必填）")
    parser.add_argument("--mapping", default=str(DEFAULT_MAPPING), help="映射 JSON 路径")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只打印报告）")
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help=(
            "允许对 qa_metadata 写。⚠️ 守卫只看库名：dev 与 prod 同名，"
            "本开关 + 生产 DSN 会真的写生产 —— 执行前必须自行核对连接串"
        ),
    )
    return parser.parse_args(argv)


def _loadMapping(path: str) -> tuple[tuple[AliasEntry, ...], tuple[ExcludedTarget, ...]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AliasBackfillError(f"映射读取失败：{path}（{exc}）") from None
    return parseMapping(raw), parseExcluded(raw)


async def _alembicRevision(session: AsyncSession) -> str:
    """备份文件名用的 revision（读不到 ⇒ ``unknown``，不阻断备份）。"""
    try:
        value = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    except Exception:  # noqa: BLE001 — 缺表/权限问题都不应阻断备份流程
        await session.rollback()
        return "unknown"
    return str(value)


async def _ensureProdBackup(
    session: AsyncSession, databaseUrl: str, database: str
) -> None:
    """闸 4：写正式库前必须完成当日备份（缺失即 ``pg_dump``，拿不到就拒绝写）。"""
    revision = await _alembicRevision(session)
    backups = ensureBackups(databaseUrl, _BACKUP_TABLES, revision, _BACKUP_DIR)
    print("=" * 72)
    print(f"⚠️ 正在写正式库 {database}（prod 同源）")
    for path in backups:
        print(f"   备份：{path}（{path.stat().st_size} bytes）")
    print("=" * 72)


async def _main(argv: Sequence[str] | None = None) -> int:
    args = _parseArgs(argv)
    targetDb = assertSafeTarget(
        args.database_url, allowProdWrite=args.allow_prod_write or not args.apply
    )
    try:
        entries, excluded = _loadMapping(args.mapping)
    except AliasBackfillError as exc:
        print(f"[refused] {exc}", file=sys.stderr)
        return _EXIT_REFUSED

    engine = create_async_engine(args.database_url)
    sessionFactory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionFactory() as session:
            dbNameRow = (await session.execute(text("SELECT current_database()"))).first()
            dbName = "" if dbNameRow is None else str(dbNameRow[0])
            if dbName != targetDb:
                print(
                    f"[refused] 实际连接库 {dbName!r} 与 DSN 目标 {targetDb!r} 不一致",
                    file=sys.stderr,
                )
                return _EXIT_REFUSED
            print(f"target: {maskDsn(args.database_url)} database={dbName}")
            if args.apply and dbName == PROD_DB_NAME:
                # 闸 4：正式库（prod 同源）写库前必须有当日备份，拿不到就拒绝。
                await _ensureProdBackup(session, args.database_url, dbName)
            try:
                return await _runPlan(session, args, entries, excluded)
            except AliasBackfillError as exc:
                print(f"[refused] {exc}", file=sys.stderr)
                return _EXIT_APPLY_FAILED if args.apply else _EXIT_REFUSED
    finally:
        await engine.dispose()


async def _runPlan(
    session: AsyncSession,
    args: argparse.Namespace,
    entries: tuple[AliasEntry, ...],
    excluded: tuple[ExcludedTarget, ...],
) -> int:
    properties = await _loadProperties(session, {entry.class_name for entry in entries})
    try:
        plans = planAliasFill(entries, properties)
        # 覆盖率：每个被引用的 (源类, 物理列) 必须在 entries ∪ excluded 里。
        assertMappingCoverage(entries, excluded, await _loadReferencedTargets(session))
    except AliasBackfillError as exc:
        print(f"[refused] {exc}", file=sys.stderr)
        return _EXIT_REFUSED

    scopes = await _loadScopes(session)
    collisions = findScopeCollisions(plans, scopes, await _loadExistingAliases(session))
    print(renderReport(plans, collisions))
    introduced = [collision for collision in collisions if collision.isIntroduced]
    if introduced:
        print("[refused] 回填会造出作用域内 alias 歧义，拒绝写库", file=sys.stderr)
        return _EXIT_REFUSED

    simulation = await simulateVerdicts(session, plans)
    text_, changed = _renderSimulation(simulation)
    print(text_)
    if changed:
        print("[refused] 回填会改变对账结论，拒绝写库", file=sys.stderr)
        return _EXIT_REFUSED
    fillTargets = {
        (plan.class_name.strip().upper(), plan.source_column.strip().upper())
        for plan in plans
        if plan.has_change
    }
    if fillTargets:
        expected = {
            kpiCode
            for kpiCode, columns in scopes.items()
            if fillTargets
            & {(name.strip().upper(), column.strip().upper()) for name, column in columns}
        }
        missingSimulation = expected - {str(row["kpiCode"]) for row in simulation}
        if not simulation or missingSimulation:
            # 空集 / 漏条 ⇒ 模拟没有证明任何事，不能当通过（假绿）。
            detail = "、".join(sorted(missingSimulation)) or "无 KPI 参与"
            print(
                f"[refused] 模拟对账覆盖不足（{detail}）⇒ 拒绝写库",
                file=sys.stderr,
            )
            return _EXIT_REFUSED

    if not args.apply:
        print("dry-run：未写库（加 --apply 落库；按批次决定，须与 A9 发布同批）")
        return 0
    try:
        count = await applyPlans(session, plans)
    except AliasBackfillError as exc:
        print(f"[refused] 落库复核失败（已回滚）：{exc}", file=sys.stderr)
        return _EXIT_APPLY_FAILED
    except Exception as exc:  # noqa: BLE001 — DB 级异常不能混进退出码 1
        print(f"[refused] 落库异常（已回滚）：{exc!r}", file=sys.stderr)
        return _EXIT_APPLY_FAILED
    print(f"已落库：{count} 行（重跑应返回 0）")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
