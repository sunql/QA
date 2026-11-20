"""M2′-1：8 条 KPI 指标 execution_source ESCAPE_HATCH → COMPILED（数据批）。

解决什么（M2′-1，落地计划 §M2′）：
- 现状：`ontology_metric` 8 条 ESCAPE_HATCH（formula 原文即 SQL，P1/P2 形态）+ 4 条 NONE。
- 目标：结构化数据已就位（source 行 18 条，A6 归因 8 pass/0 mismatch）⇒ 把可编译的
  指标翻转为 **COMPILED**（P3 编译器形态）——口径成为可编译的数据，而非逃生舱文本。

四道护栏（沿用 kpi_metric_backfill 的闸门纪律）：
1. **显式目标**：只认 ``--database-url``，不读 DATABASE_URL/TEST_DATABASE_URL；
2. **库名白名单 + 回环 host**：仅 `qa_metadata`（host 必须回环）或
   `qa_metadata_test*`（任意 host）；其余拒绝；
3. **逐条前置门**（任一不过 ⇒ 该指标跳过并留痕，不盲翻）：
   a. `loadAndCompileMetricSql`（运行时同路径）编译成功（fail-closed reason 即拒绝）；
   b. 层 3 对账 `reconcileExecutableVsStructured` **无 MISMATCH**（翻转前该检查
      只对 ESCAPE_HATCH 生效 ⇒ 必须在翻转前做，翻了就再也比对不了）；
4. **幂等**：已是 COMPILED ⇒ 跳过；零翻转 ⇒ 跳过域重发布；重复运行 = no-op。

发布联动：翻转改变 manifest 内容（metrics 段）⇒ 默认随后重建 PROCUREMENT 域发布
（createDraft → markValidated → activate；--skip-release 跳过；内容与 ACTIVE 一致
⇒ 不重复激活）。

实测结论（2026-11-20，dev 5433）：**3 条翻转成功**（OTD / PAYMENT_ON_TIME /
OVERDUE_RATIO——编译 ok + 层 3 无 MISMATCH），**5 条为编译器能力边界**（4 条跨表
表达式 `compiled_measure_class_unresolved`——R./O./P. 前缀横跨多表，编译器单类
表达式模型不支持，`COMPILED_EXPR_CROSS_TABLE_UNSUPPORTED` 预留码；1 条 FILTER 源
`compiled_filter_source_unsupported`）⇒ 保留 ESCAPE_HATCH，等编译器扩列另批。

用法（backend/ 下）::

    uv run python scripts/m2p1_flip_metric_execution_source.py \\
        --database-url 'postgresql+asyncpg://user:pw@localhost:5433/qa_metadata'          # dry-run
    uv run python scripts/m2p1_flip_metric_execution_source.py --apply \\
        --database-url '...'
    uv run python scripts/m2p1_flip_metric_execution_source.py --apply --skip-release \\
        --database-url '...'
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
from app.domain.models import DataSource, OntologyMetric  # noqa: E402
from app.services.kpi_metric_reconcile import (  # noqa: E402
    VERDICT_MISMATCH,
)
from app.services.kpi_metric_reconcile import (
    reconcileExecutableVsStructured as _layer3Reconcile,
)
from app.services.kpi_metric_spec import techniqueOf  # noqa: E402
from app.services.kpi_sql_compiler import loadAndCompileMetricSql  # noqa: E402
from app.services.semantic_registry_service import semanticRegistry  # noqa: E402
from app.services.semantic_release_service import semanticReleaseService  # noqa: E402

ACTOR = "m2p1-flip"
RELEASE_VERSION = "v-m2p1-compiled-1"
RELEASE_NOTE = (
    "M2′-1：KPI 指标 execution_source ESCAPE_HATCH→COMPILED"
    "（编译前置门全过；Harness/changes/2026-11-20-m2p1-compiled-flip）"
)

_ALLOWED = ("qa_metadata",)


def _assertSafeTarget(databaseUrl: str) -> None:
    """护栏 2：库名白名单 + 回环 host（翻 ontology_metric 治理数据，宁窄勿宽）。"""
    parsed = urlparse(databaseUrl)
    dbName = parsed.path.lstrip("/")
    host = parsed.hostname or ""
    if dbName not in _ALLOWED and not dbName.startswith("qa_metadata_test"):
        raise SystemExit(f"拒绝运行：库名 {dbName!r} 不在白名单（{_ALLOWED} / qa_metadata_test*）")
    if dbName == "qa_metadata" and host not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit(f"拒绝运行：qa_metadata 仅允许回环 host，实际 {host!r}")


async def flipOne(
    session: AsyncSession,
    metric: OntologyMetric,
    datasourceType: str,
    dryRun: bool,
) -> str:
    """单指标：编译前置门 + 层 3 对账 ⇒ 翻转。返回动作摘要。"""
    if metric.execution_source == MetricExecutionSource.COMPILED.value:
        return f"[skip] {metric.metric_name}：已是 COMPILED"
    if metric.execution_source != MetricExecutionSource.ESCAPE_HATCH.value:
        return f"[skip] {metric.metric_name}：source={metric.execution_source}（非 ESCAPE_HATCH，不在本批范围）"

    # 前置门 a：运行时同路径编译必须成功
    compiled = await loadAndCompileMetricSql(
        session, metric, datasourceType=datasourceType
    )
    if not compiled.ok:
        return (
            f"[abort] {metric.metric_name}：编译 fail-closed reason={compiled.reason} "
            "⇒ 不翻转（修数据后再跑）"
        )

    # 前置门 b：层 3 对账（仅 ESCAPE_HATCH 可比 ⇒ 必须翻前做）
    technique = techniqueOf(metric)
    layer3 = _layer3Reconcile(technique)
    mismatch = [f for f in layer3.findings if f.verdict == VERDICT_MISMATCH]
    if mismatch:
        return (
            f"[abort] {metric.metric_name}：层 3 对账 MISMATCH "
            f"{[m.reason for m in mismatch]} ⇒ 不翻转"
        )

    if dryRun:
        return (
            f"[plan] {metric.metric_name}：编译 ok（len={len(compiled.sql or '')}）"
            " + 层 3 无 MISMATCH ⇒ 可翻转"
        )
    await session.execute(
        update(OntologyMetric)
        .where(OntologyMetric.id == metric.id)
        .values(execution_source=MetricExecutionSource.COMPILED.value)
    )
    return f"[flip] {metric.metric_name}（id={metric.id}）：ESCAPE_HATCH → COMPILED"


async def rereleaseDomain(session: AsyncSession, dryRun: bool) -> None:
    """PROCUREMENT 域重发布（metrics 段内容已变 ⇒ manifest 必须重编译）。"""
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
    op = await semanticReleaseService.markValidated(
        session, releaseId, actor=ACTOR
    )
    for blocker in op.blockers:
        print(f"[blocker] {blocker[:200]}")
    # 幂等：新草稿与当前 ACTIVE 内容一致 ⇒ 不重复激活（草稿留作预览）
    if active is not None and op.release.manifest_hash == active.manifest_hash:
        print(
            f"[release] 内容与 ACTIVE id={active.id} 一致"
            f"（hash={op.release.manifest_hash[:12]}…）⇒ 跳过激活（草稿留作预览）"
        )
        return
    await semanticReleaseService.activate(session, releaseId, actor=ACTOR)
    active = await semanticRegistry.getActive(session, domainCode="PROCUREMENT")
    metrics = active.manifest.get("metrics") or []
    print(
        f"[release] ACTIVE id={active.id} metrics={len(metrics)}"
        "（manifest 不携带 executionSource ⇒ 以库为准）"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--apply", action="store_true", help="真写（缺省 dry-run）")
    parser.add_argument("--skip-release", action="store_true")
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

        ds = (
            await session.execute(select(DataSource).limit(1))
        ).scalars().first()
        datasourceType = ds.type if ds is not None else "oracle"

        metrics = (
            await session.execute(
                select(OntologyMetric).where(
                    OntologyMetric.execution_source
                    == MetricExecutionSource.ESCAPE_HATCH.value
                )
            )
        ).scalars().all()
        print(f"[scan] ESCAPE_HATCH 指标 {len(metrics)} 条（dry-run={dryRun}）")
        flipped = 0
        for metric in metrics:
            line = await flipOne(session, metric, datasourceType, dryRun)
            print(line)
            if line.startswith("[flip]"):
                flipped += 1
        if not dryRun and flipped:
            await session.commit()
            print(f"[apply] 已翻转 {flipped} 条")
        if args.skip_release:
            print("[release] --skip-release ⇒ 跳过域重发布")
            return
        if flipped == 0 and not dryRun:
            print("[release] 零翻转 ⇒ manifest 内容未变 ⇒ 跳过域重发布")
            return
        await rereleaseDomain(session, dryRun)


if __name__ == "__main__":
    asyncio.run(main())
