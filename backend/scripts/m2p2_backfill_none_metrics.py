"""M2′-2：4 条 NONE 指标补技术形态（数据批，口径来源 = G1 摄入包）。

解决什么（M2′-2，落地计划 §M2′）：4 条 `execution_source=NONE` 的 KPI 指标
（formula 是口径声明文本 ⇒ 运行时 LLM 猜）。技术形态的**权威来源** =
`docs/update/1118/M0/answers/kpi-slots-filled.json`（A9–A12 问卷答案 +
源侧实测 + 冲突/降级裁决）。

**逐条裁决（答案包背书，不自由发挥）**：

| 指标 | 裁决 | 依据 |
|---|---|---|
| `KPI_SUPPLIER_DEFECT_RATE`（id 11） | **回填**：分子/分母 confirmed（同类 DWD_ARRIVAL_ORDER_DTL，实测 81,754 行/列 100% 填充）+ time_column=ARR_DATE confirmed ⇒ 编译门过 ⇒ 翻 COMPILED | A9.1–A9.4 |
| `KPI_SUPPLIER_FPY`（id 12） | **skip-blocked**：分母单位待回问（C-05/Q6——A10.3 说与 A9 互为补集但 A10.2 说分母=行数，单位不一致） | A10 |
| `KPI_SUPPLIER_MAINTENANCE_RATIO`（id 10） | **skip-blocked**：R-08 裁决降级『准入台账只读展示』（品类准入 0 行/码值未确认/银行信息缺失/资质仅 6 行）+ 停用列不存在（C-14/Q9） | A12 |
| `KPI_SUPPLIER_NCR_RATE`（id 13） | **skip-blocked**：A11.1 治理侧摘除首期清单，ontology_metric 保持 DRAFT（不删） | A11 |

护栏与 M2′-1 同款：显式 `--database-url` / 库名白名单+回环 host / **编译前置门**
（`loadAndCompileMetricSql` 运行时同路径，fail-closed 即 abort 不翻转）/ 幂等
（已回填+已翻转 ⇒ no-op）/ 域重发布联动（零翻转 ⇒ 跳过）。

expr 形态注意：答案原文带表别名前缀（`A.ARR_QTY`）——编译器按列标识符渲染
（qualifier 由编译器统一加表前缀）⇒ 回填**剥离别名前缀**，语义逐字保留
（`ARR_QTY - QUALIFIED_QTY` / `ARR_QTY`）。

用法（backend/ 下）::

    uv run python scripts/m2p2_backfill_none_metrics.py \\
        --database-url 'postgresql+asyncpg://user:pw@localhost:5433/qa_metadata'   # dry-run
    uv run python scripts/m2p2_backfill_none_metrics.py --apply --database-url '...'
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.domain.enums import MetricExecutionSource  # noqa: E402
from app.domain.models import (  # noqa: E402
    DataSource,
    KpiCatalog,
    OntologyMetric,
    OntologyMetricSource,
)
from app.services.kpi_sql_compiler import loadAndCompileMetricSql  # noqa: E402
from app.services.semantic_registry_service import semanticRegistry  # noqa: E402
from app.services.semantic_release_service import semanticReleaseService  # noqa: E402

ACTOR = "m2p2-backfill"
RELEASE_VERSION = "v-m2p2-defect-rate-1"
RELEASE_NOTE = (
    "M2′-2：KPI_SUPPLIER_DEFECT_RATE 技术形态回填（A9 答案背书）NONE→COMPILED；"
    "其余 3 条按 G1 裁决 skip-blocked（Harness/changes/2026-11-20-m2p2-none-metrics）"
)

_ALLOWED = ("qa_metadata",)

#: DEFECT_RATE（id 11）的技术形态（A9.1–A9.4 答案 + 实测，逐字保留语义、剥离表别名）。
_DEFECT_RATE = {
    "metric_id": 11,
    "class_id": 6,
    "numerator": {"agg": "SUM", "expr": "ARR_QTY - QUALIFIED_QTY", "class_id": 6},
    "denominator": {"agg": "SUM", "expr": "ARR_QTY", "class_id": 6},
    "grain_keys": ["SUPPLIER_CODE", "MATERIAL_CODE", "FACILITY_CODE"],
    "time_grain": "month",
    "time_column": "ARR_DATE",
}

#: 治理侧列（kpi_catalog.numerator/denominator_columns——层 2 对账的签字基准；
#: 空数组 + 技术侧非空 ⇒ 对账矛盾 ⇒ Tier B。格式对齐 Tier A 既有行：纯列名清单）。
_DEFECT_RATE_GOVERNANCE = {
    "numerator_columns": ["ARR_QTY", "QUALIFIED_QTY"],
    "denominator_columns": ["ARR_QTY"],
    "grain_keys": ["SUPPLIER_CODE", "MATERIAL_CODE", "FACILITY_CODE"],
}

_SKIP_REASONS = {
    "KPI_SUPPLIER_MAINTENANCE_RATIO": "R-08 降级『准入台账只读展示』+ 停用列不存在（C-14/Q9）⇒ blocked",
    "KPI_SUPPLIER_FPY": "分母单位待回问（C-05/Q6：A10.2 行数 vs A10.3 与 A9 互为补集矛盾）⇒ blocked",
    "KPI_SUPPLIER_NCR_RATE": "A11.1 治理侧摘除首期清单 ⇒ 保持 DRAFT/NONE（不删）",
}


def _assertSafeTarget(databaseUrl: str) -> None:
    parsed = urlparse(databaseUrl)
    dbName = parsed.path.lstrip("/")
    host = parsed.hostname or ""
    if dbName not in _ALLOWED and not dbName.startswith("qa_metadata_test"):
        raise SystemExit(f"拒绝运行：库名 {dbName!r} 不在白名单（{_ALLOWED} / qa_metadata_test*）")
    if dbName == "qa_metadata" and host not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit(f"拒绝运行：qa_metadata 仅允许回环 host，实际 {host!r}")


async def backfillGovernance(session: AsyncSession, metricId: int, dryRun: bool) -> bool:
    """kpi_catalog 治理列回填（层 2 对账的签字基准；幂等：非空不覆盖）。返回是否写入。"""
    row = (
        await session.execute(
            select(KpiCatalog).where(KpiCatalog.metric_id == metricId)
        )
    ).scalars().first()
    if row is None:
        return False
    needNumerator = not row.numerator_columns
    needDenominator = not row.denominator_columns
    needGrain = not row.grain_keys
    if not (needNumerator or needDenominator or needGrain):
        return False
    if dryRun:
        print(f"[plan] 治理列回填 kpi={row.kpi_code}")
        return True
    await session.execute(
        update(KpiCatalog)
        .where(KpiCatalog.id == row.id)
        .values(
            numerator_columns=(
                _DEFECT_RATE_GOVERNANCE["numerator_columns"] if needNumerator
                else row.numerator_columns
            ),
            denominator_columns=(
                _DEFECT_RATE_GOVERNANCE["denominator_columns"] if needDenominator
                else row.denominator_columns
            ),
            grain_keys=(
                _DEFECT_RATE_GOVERNANCE["grain_keys"] if needGrain else row.grain_keys
            ),
        )
    )
    print(f"[governance] {row.kpi_code}：治理列已回填（num/den/grain_keys 缺项补齐）")
    return True


async def backfillDefectRate(
    session: AsyncSession, metric: OntologyMetric, datasourceType: str, dryRun: bool
) -> tuple[str, bool]:
    """metric 11 自愈式回填（幂等）：结构化缺口 → 编译门 → formula=编译 SQL → COMPILED → 治理列。

    返回 (动作摘要, 是否发生写入)。
    """
    mutated = False
    if metric.execution_source not in (
        MetricExecutionSource.NONE.value,
        MetricExecutionSource.COMPILED.value,
    ):
        return f"[skip] {metric.metric_name}：source={metric.execution_source}（不在本批）", False

    needMeasures = not metric.numerator_measure
    if needMeasures and not dryRun:
        await session.execute(
            update(OntologyMetric)
            .where(OntologyMetric.id == metric.id)
            .values(
                numerator_measure=_DEFECT_RATE["numerator"],
                denominator_measure=_DEFECT_RATE["denominator"],
                grain_keys=_DEFECT_RATE["grain_keys"],
                time_grain=_DEFECT_RATE["time_grain"],
                time_column=_DEFECT_RATE["time_column"],
                execution_source=MetricExecutionSource.ESCAPE_HATCH.value,
            )
        )
        for role in ("NUMERATOR", "DENOMINATOR"):
            exists = (
                await session.execute(
                    select(OntologyMetricSource.id).where(
                        OntologyMetricSource.metric_id == metric.id,
                        OntologyMetricSource.class_id == _DEFECT_RATE["class_id"],
                        OntologyMetricSource.role == role,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(
                    OntologyMetricSource(
                        metric_id=metric.id,
                        class_id=_DEFECT_RATE["class_id"],
                        role=role,
                    )
                )
        await session.flush()
        mutated = True

    # 编译门（每次运行都跑：formula 自愈与翻转都以它为准）
    compiled = await loadAndCompileMetricSql(
        session, metric, datasourceType=datasourceType
    )
    if not compiled.ok:
        if mutated:
            await session.rollback()
        return (
            f"[abort] {metric.metric_name}：编译 fail-closed reason={compiled.reason} "
            "⇒ 已回滚（数据不动，修口径后再跑）"
        ), mutated

    # formula 自愈：可执行指标的 formula = 编译 SQL（层 1 对账对声明文本必误判；
    # 编译 SQL 即 A9 签署语义的逐字渲染 ⇒ 语义一致由答案背书）
    if (metric.formula or "").strip() != (compiled.sql or "").strip():
        if not dryRun:
            await session.execute(
                update(OntologyMetric)
                .where(OntologyMetric.id == metric.id)
                .values(
                    formula=compiled.sql,
                    execution_source=MetricExecutionSource.COMPILED.value,
                )
            )
            mutated = True
            govWritten = await backfillGovernance(session, metric.id, dryRun)
            return f"[formula] {metric.metric_name}：formula 替换为编译 SQL（len={len(compiled.sql or '')}）+ 治理列已核对", mutated or govWritten
        return f"[plan/formula] {metric.metric_name}：formula 将替换为编译 SQL（len={len(compiled.sql or '')}）", mutated

    if metric.execution_source != MetricExecutionSource.COMPILED.value:
        if not dryRun:
            await session.execute(
                update(OntologyMetric)
                .where(OntologyMetric.id == metric.id)
                .values(execution_source=MetricExecutionSource.COMPILED.value)
            )
            mutated = True
        return f"[flip] {metric.metric_name}（id={metric.id}）：NONE → COMPILED（编译 ok）", mutated

    govWritten = await backfillGovernance(session, metric.id, dryRun)
    return f"[ok] {metric.metric_name}：COMPILED + formula/治理列已就绪", mutated or govWritten


async def rereleaseDomain(session: AsyncSession, dryRun: bool) -> None:
    active = await semanticRegistry.getActive(session, domainCode="PROCUREMENT")
    if dryRun:
        print(f"[plan] 域重发布 version={RELEASE_VERSION}（当前 ACTIVE id={active.id if active else None}）")
        return
    op = await semanticReleaseService.createDraft(
        session,
        version=RELEASE_VERSION,
        domainCode="PROCUREMENT",
        actor=ACTOR,
        note=RELEASE_NOTE,
    )
    releaseId = op.release.id
    print(f"[release] DRAFT id={releaseId}")
    op = await semanticReleaseService.markValidated(session, releaseId, actor=ACTOR)
    for blocker in op.blockers:
        print(f"[blocker] {blocker[:200]}")
    if active is not None and op.release.manifest_hash == active.manifest_hash:
        print(f"[release] 内容与 ACTIVE id={active.id} 一致 ⇒ 跳过激活")
        return
    await semanticReleaseService.activate(session, releaseId, actor=ACTOR)
    active = await semanticRegistry.getActive(session, domainCode="PROCUREMENT")
    print(f"[release] ACTIVE id={active.id} metrics={len(active.manifest.get('metrics') or [])}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--apply", action="store_true", help="真写（缺省 dry-run）")
    args = parser.parse_args()
    dryRun = not args.apply
    _assertSafeTarget(args.database_url)

    from scripts.db_snapshot_guard import buildSessionFactory

    factory = buildSessionFactory(args.database_url)
    async with factory() as session:
        current = (
            await session.execute(text("SELECT current_database()"))
        ).scalar_one()
        assert current in _ALLOWED or current.startswith("qa_metadata_test"), current

        ds = (await session.execute(select(DataSource).limit(1))).scalars().first()
        datasourceType = ds.type if ds is not None else "oracle"

        metrics = (
            await session.execute(
                select(OntologyMetric).where(
                    OntologyMetric.execution_source
                    == MetricExecutionSource.NONE.value
                )
            )
        ).scalars().all()
        print(f"[scan] NONE 指标 {len(metrics)} 条（dry-run={dryRun}）")
        # 目标集合 = NONE 指标 + DEFECT_RATE（COMPILED 后的治理列幂等补齐路径）
        defect = (
            await session.execute(
                select(OntologyMetric).where(
                    OntologyMetric.metric_name.startswith("KPI_SUPPLIER_DEFECT_RATE")
                )
            )
        ).scalars().all()
        seenIds = {m.id for m in metrics}
        metrics = list(metrics) + [m for m in defect if m.id not in seenIds]
        flipped = 0
        for metric in metrics:
            skipReason = next(
                (r for code, r in _SKIP_REASONS.items() if metric.metric_name.startswith(code)),
                None,
            )
            if skipReason is not None:
                print(f"[skip-blocked] {metric.metric_name}：{skipReason}")
                continue
            if metric.metric_name.startswith("KPI_SUPPLIER_DEFECT_RATE"):
                line, mutated = await backfillDefectRate(session, metric, datasourceType, dryRun)
                print(line)
                if mutated:
                    flipped += 1
            else:
                print(f"[skip] {metric.metric_name}：无 G1 裁决条目 ⇒ 不动")
        if not dryRun and flipped:
            await session.commit()
            print(f"[apply] 写入 {flipped} 项（翻转/formula/治理列）")
            await rereleaseDomain(session, dryRun)
        elif not dryRun:
            print("[release] 零写入 ⇒ 跳过域重发布")


if __name__ == "__main__":
    asyncio.run(main())
