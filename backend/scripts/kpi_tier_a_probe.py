#!/usr/bin/env python3
"""Tier A 真实数据验收探针（手动、不进 CI；零生产写操作）。

- ``--dry-run``（默认）：只读元数据库，打印 KPI 的 ``tier/reason``；
- ``--execute --kpi-code KPI_XXX --datasource-id N --confirm-target``：直接调用
  ``KpiExecutionService.execute``（不经过 chat/LLM，不读 Tier A 开关行）。
  缺 ``--confirm-target`` 时只打印业务库目标（type/host/port/database，不含
  用户名/口令）并以退出码 3 结束，**不会执行查询**。

Tier 判定与运行时同源：KpiCatalog → OntologyMetric → ``governanceSpecOf`` +
``techniqueOf`` → ``reconcileMetric`` → ``isTierAEligible`` → ``planKpiExecution``；
``switch_on`` 固定 True，输出标注 ``switch=forced_on``。

目标库白名单：``qa_metadata_test*``（任意 host），或**仅回环 host** 的
``qa_metadata``；其他拒绝（退出 2，只打印库名/host，绝不打印密码）。
库名与 host 只从 authority/path 读取；**任何非空 query 一律拒绝**（``?host=`` /
``?database=`` 会被 ``create_async_engine`` 合并进连接参数），只打印 key 列表。

退出码：``--execute`` 成功 0，失败 1（含设计内降级的 ``empty_result``）；缺
``--confirm-target`` 时 3（未执行查询）；调用方应按输出 ``status``
（ok / expected_degradation(...) / failed(...)）判定。

示例（在 backend/ 下执行；URL 用占位符，勿把口令写进 shell 历史/证据文件）::

    python -m scripts.kpi_tier_a_probe --database-url "$QA_METADATA_URL"
    python -m scripts.kpi_tier_a_probe --database-url "$QA_METADATA_URL" \\
        --execute --kpi-code KPI_SUPPLIER_OTD --datasource-id 1 --confirm-target
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# 允许 `python scripts/kpi_tier_a_probe.py` 直接运行：backend/ 进 sys.path，
# 必须在任何 `import app.*` 之前执行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.infrastructure.business_db_pool as _businessDbPool
from app.domain.models import DataSource, KpiCatalog, OntologyMetric
from app.services.kpi_execution_service import (
    TIER_A,
    KpiExecutionPlan,
    KpiExecutionService,
    planKpiExecution,
)
from app.services.kpi_metric_reconcile import reconcileMetric
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf

_TEST_DB_PATTERN = re.compile(r"^qa_metadata_test($|_)")
_PROD_DB_NAME = "qa_metadata"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def assertAllowedTarget(databaseUrl: str) -> tuple[str, str]:
    """校验目标库；返回 (库名, host)。拒绝退出 2，不泄漏口令/完整 DSN。

    库名与 host 只从 authority/path 读取；任何非空 query 一律拒绝（可绕过
    库/主机护栏），且只打印 key 列表，不打印值（值可能是口令类）。
    """
    try:
        parsed = make_url(databaseUrl)
    except Exception:
        print("拒绝执行：--database-url 无法解析", file=sys.stderr)
        raise SystemExit(2) from None
    if parsed.query:
        print(
            "拒绝执行：连接参数中不允许 query 参数（可绕过库/主机护栏）："
            f"keys={sorted(parsed.query.keys())}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    name = (parsed.database or "").strip()
    host = (parsed.host or "").strip().strip("[]").lower()
    if _TEST_DB_PATTERN.match(name):
        return name, host
    if name == _PROD_DB_NAME and host in _LOOPBACK_HOSTS:
        return name, host
    print(
        f"拒绝执行：database={name!r} host={host!r}。"
        f"只允许 qa_metadata_test*（任意 host），或回环 host 上的 qa_metadata。",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _requireAsyncUrl(databaseUrl: str) -> None:
    if not databaseUrl.startswith("postgresql+asyncpg://"):
        print("拒绝执行：--database-url 必须是 postgresql+asyncpg:// 异步 DSN。", file=sys.stderr)
        raise SystemExit(2)


def planFor(kpi: KpiCatalog, metric: OntologyMetric | None) -> KpiExecutionPlan:
    """运行时口径：reconcileMetric → isTierAEligible → planKpiExecution。

    与 ``chat_stream_output._planKpiTier`` 同形：metric 缺失时不做对账，eligible 置 False。
    """
    reconcile = (
        reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))
        if metric is not None else None
    )
    return planKpiExecution(
        # 探针明确不读灰度开关行：强制 on，输出里标注 switch=forced_on。
        switch_on=True,
        kpi_found=True,
        metric_present=metric is not None,
        execution_source=(metric.execution_source if metric is not None else None),
        tier_a_eligible=bool(reconcile is not None and reconcile.isTierAEligible),
        has_datasource=kpi.datasource_id is not None,
        formula_present=bool(metric is not None and (metric.formula or "").strip()),
        # 探针没有问句；显式时间限定由 chat 侧 hasExplicitTimeScope 判定。
        has_explicit_time_scope=False,
    )


async def _loadKpis(session: AsyncSession, kpiCode: str | None) -> list[KpiCatalog]:
    stmt = select(KpiCatalog).order_by(KpiCatalog.kpi_code)
    if kpiCode:
        stmt = stmt.where(KpiCatalog.kpi_code == kpiCode)
    rows = await session.execute(stmt)
    return list(rows.scalars().all())


async def _metricOf(session: AsyncSession, kpi: KpiCatalog) -> OntologyMetric | None:
    if kpi.metric_id is None:
        return None
    return await session.get(OntologyMetric, kpi.metric_id)


def _sourceOf(metric: OntologyMetric | None) -> str:
    return (metric.execution_source if metric is not None else None) or "-"


async def runDryRun(session: AsyncSession, kpiCode: str | None) -> int:
    kpis = await _loadKpis(session, kpiCode)
    if kpiCode and not kpis:
        print(f"未找到 KPI: {kpiCode}", file=sys.stderr)
        return 1
    print("switch=forced_on (未读取 Tier A 开关行)")
    tierACount = 0
    for kpi in kpis:
        metric = await _metricOf(session, kpi)
        plan = planFor(kpi, metric)
        tierACount += int(plan.tier == TIER_A)
        print(
            f"kpi_code={kpi.kpi_code} tier={plan.tier} "
            f"reason={plan.reason or '-'} source={_sourceOf(metric)} "
            f"eligible={plan.tier == TIER_A} datasource_id={kpi.datasource_id}"
        )
    print(f"total={len(kpis)} tierA={tierACount}")
    return 0


def _printBusinessTarget(datasource: DataSource) -> None:
    """打印业务库目标（type/host/port/database）。绝不打印用户名/口令。"""
    rawType = getattr(datasource.type, "value", datasource.type)
    print(
        f"业务库目标：type={str(rawType).upper()} host={datasource.host} "
        f"port={datasource.port} database={datasource.database_name}"
    )


async def runExecute(
    session: AsyncSession,
    kpiCode: str,
    datasourceId: int,
    *,
    confirmTarget: bool = False,
) -> int:
    """执行单条 Tier A KPI。成功 0；失败 1；缺目标确认 3（不执行查询）。"""
    kpis = await _loadKpis(session, kpiCode)
    if not kpis:
        print(f"未找到 KPI: {kpiCode}", file=sys.stderr)
        return 1
    kpi = kpis[0]
    metric = await _metricOf(session, kpi)
    plan = planFor(kpi, metric)
    print(f"switch=forced_on (未读取 Tier A 开关行) kpi_code={kpi.kpi_code} "
          f"tier={plan.tier} reason={plan.reason or '-'} source={_sourceOf(metric)}")
    if plan.tier != TIER_A:
        print(f"拒绝执行：该 KPI 路由不是 Tier A（tier={plan.tier}）。", file=sys.stderr)
        return 1
    if kpi.datasource_id is None:
        print("拒绝执行：KPI 未绑定 datasource_id。", file=sys.stderr)
        return 1
    if datasourceId != kpi.datasource_id:
        # 运行时只认 kpi.datasource_id；显式参数不一致说明调用方搞错了目标。
        print(
            f"拒绝执行：--datasource-id={datasourceId} 与 KPI 绑定值 "
            f"{kpi.datasource_id} 不一致。",
            file=sys.stderr,
        )
        return 1
    datasource = await session.get(DataSource, datasourceId)
    if datasource is None:
        print(f"未找到 data_source id={datasourceId}", file=sys.stderr)
        return 1

    # 安全终审 M4：--execute 必须显式确认业务库目标，避免按错 datasource-id
    # 把查询打到别的库。确认前不改 evidence 钩子、不构造执行服务。
    if not confirmTarget:
        _printBusinessTarget(datasource)
        print(
            "拒绝执行：未确认业务库目标；请核对上方 type/host/port/database 后"
            "加 --confirm-target 才会真正执行。",
            file=sys.stderr,
        )
        return 3

    # 零写库：抑制两类 evidence。METRIC_RESULT 由 service 注入 noop；SQL_QUERY 由
    # adapter 在查询成功后调用模块属性 scheduleSqlQueryEvidence（调用时解析），
    # 故临时替换为计数 noop，finally 还原。
    suppressedEvidence = 0

    def _suppressSqlQueryEvidence(**_kwargs: Any) -> None:
        nonlocal suppressedEvidence
        suppressedEvidence += 1

    originalSchedule = _businessDbPool.scheduleSqlQueryEvidence
    _businessDbPool.scheduleSqlQueryEvidence = _suppressSqlQueryEvidence
    try:
        service = KpiExecutionService(evidenceScheduler=lambda **_kwargs: None)
        result = await service.execute(
            formula=metric.formula,
            datasourceId=kpi.datasource_id,
            datasource=datasource,
            metricCode=kpi.kpi_code,
            metricName=kpi.kpi_name,
            confidence=1.0,
        )
    finally:
        _businessDbPool.scheduleSqlQueryEvidence = originalSchedule

    if result.ok:
        status = "ok"
    elif result.reason == "empty_result":
        status = f"expected_degradation({result.reason})"
    else:
        status = f"failed({result.reason})"
    print(json.dumps({
        "kpiCode": kpi.kpi_code,
        "ok": result.ok,
        "status": status,
        "value": _jsonSafe(result.value),
        "rowCount": result.rowCount,
        "elapsedMs": result.elapsedMs,
        "truncated": result.truncated,
        "reason": result.reason,
    }, ensure_ascii=False))
    print(f"evidence: suppressed={suppressedEvidence}")
    return 0 if result.ok else 1


def _jsonSafe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _parseArgs(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tier A 真实数据验收探针（元数据库只读；不经过 chat/LLM）。",
        epilog=(
            "验收步骤：1) 8 条 ESCAPE_HATCH 应 tier=A；2) 4 条 NONE 应 "
            "tier=C/execution_source_none；3) 逐条 --execute 记录数值；"
            "4) 同一 formula 用只读客户端手工执行对比；5) diff<=0.01 记 PASS。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-url", required=True,
        help="元数据库 DSN（必填）。只允许 qa_metadata_test* 或回环 host 上的 qa_metadata。",
    )
    parser.add_argument("--kpi-code", default=None, help="KPI code；dry-run 可选，execute 必填。")
    parser.add_argument(
        "--datasource-id", type=int, default=None,
        help="业务数据源 id；execute 必填，且必须等于 KPI 绑定的 data_source.id。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只打印路由计划（默认）。")
    mode.add_argument("--execute", action="store_true", help="真实执行指定 KPI。")
    parser.add_argument(
        "--confirm-target",
        action="store_true",
        help=(
            "确认业务库目标后才真正执行（--execute 用）。缺省时只打印 "
            "type/host/port/database 并退出 3，不执行查询。"
        ),
    )
    args = parser.parse_args(argv)
    if args.execute and not args.kpi_code:
        parser.error("--execute 必须同时提供 --kpi-code（不允许全量执行）")
    if args.execute and args.datasource_id is None:
        parser.error("--execute 必须同时提供 --datasource-id")
    return args


async def _run(args: argparse.Namespace) -> int:
    _requireAsyncUrl(args.database_url)
    assertAllowedTarget(args.database_url)
    engine = create_async_engine(args.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            if args.execute:
                return await runExecute(
                    session,
                    args.kpi_code,
                    args.datasource_id,
                    confirmTarget=args.confirm_target,
                )
            return await runDryRun(session, args.kpi_code)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run(_parseArgs(argv)))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 手动探针：显式报错并给出类型
        # 安全终审 M5：异常原文可能含内部地址/用户名等，只打印类名。
        print(f"探针失败：{type(exc).__name__}（详情已省略）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
