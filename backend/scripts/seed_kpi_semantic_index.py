"""Task 1.5 KPI semantic index 种子脚本（幂等）。

12 个核心采购域 KPI 的 L1 语义匹配关键词：

交付类（4）：OTD / Overdue Ratio / Cycle Time / Delay Days
质量类（3）：Defect Rate / FPY / NCR Rate
价格类（2）：Price Variance / Cost Saving
财务/合规（3）：Invoice Match / Payment On-Time / Maintenance Ratio

幂等：只更新 status=PUBLISHED 的 KPI，重复运行覆盖旧值。
DRAFT/DEPRECATED 不被触碰。写入走 `KpiCatalogService.updateKpi`
（产出 audit_log / history / outbox + `refreshOne`），不用裸 Core UPDATE。

⚠️ **调用契约变更（FIX-I3）**：本脚本现在带目标库守卫 ——
- 目标库必须显式传 `--database-url`（**不读** `DATABASE_URL` / `TEST_DATABASE_URL`）；
- 目标库是 `qa_metadata`（prod 同源）⇒ 必须显式 `--allow-prod-write`，
  且写库前自动完成当日 `pg_dump` 备份（复用回填脚本的 `_ensureProdBackup`）；
- `--dry-run` 只打印将更新的 KPI / 字段，不写库。

语义关键词设计原则：
- 中文 2-4 字 ngram + 英文短语
- 不填停用词
- 单 KPI 适量（4-7 个），防止退化
- 跨 KPI 有适度区分度

依赖 scripts/seed_kpi_catalog.py 先运行（12 条 PUBLISHED KPI 须先存在）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import DBAPIError, OperationalError  # noqa: E402

from app.dependencies import CurrentUser  # noqa: E402
from app.domain.enums import KpiStatus  # noqa: E402
from app.domain.models import KpiCatalog  # noqa: E402
from app.domain.schemas import KpiCatalogUpdate  # noqa: E402
from app.services.acl_service import ADMIN_ROLE  # noqa: E402
from app.services.kpi_catalog_service import KpiCatalogService  # noqa: E402
from scripts.kpi_metric_backfill import (  # noqa: E402
    _alembicRevision,
    _assertSameDatabase,
    _ensureProdBackup,
    _printBanner,
    buildSessionFactory,
    connectionErrorExit,
    maskDsn,
)
from scripts.kpi_metric_db_guard import (  # noqa: E402
    PROD_DB_NAME,
    assertSafeTarget,
)

# 12 个 KPI 的语义关键词（字段与 KpiCatalog ORM 列对齐）
# 仅更新 status=PUBLISHED 的 KPI；DRAFT/DEPRECATED 不在范围内
KPI_SEMANTIC_INDEX: list[dict[str, Any]] = [
    # ── 交付类（4）───────────────────────────────────────────────────────
    {
        "kpi_code": "KPI_SUPPLIER_OTD",
        "semantic_keywords": [
            "准时交付",
            "OTD",
            "按时交付",
            "准时率",
            "交付率",
            "on time delivery",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_OVERDUE_RATIO",
        "semantic_keywords": [
            "逾期率",
            "逾期",
            "逾期订单",
            "overdue",
            "delayed",
            "延迟交付",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_PURCHASE_CYCLE_TIME",
        "semantic_keywords": [
            "采购周期",
            "交期",
            "交货周期",
            "lead time",
            "采购交期",
            "delivery cycle",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_DELAY_DAYS",
        "semantic_keywords": [
            "延误天数",
            "平均延误",
            "delay days",
            "逾期天数",
            "延误",
            "delivery delay",
        ],
        "match_threshold": Decimal("0.75"),
    },
    # ── 质量类（3）───────────────────────────────────────────────────────
    {
        "kpi_code": "KPI_SUPPLIER_DEFECT_RATE",
        "semantic_keywords": [
            "不良率",
            "来料不良",
            "缺陷率",
            "defect rate",
            "quality defect",
            "iqc",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_FPY",
        "semantic_keywords": [
            "一次合格率",
            "FPY",
            "first pass yield",
            "首检通过",
            "一次通过率",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_NCR_RATE",
        "semantic_keywords": [
            "NCR",
            "不合格报告",
            "non-conformance",
            "来料异常",
            "质量异常",
            "NCR rate",
        ],
        "match_threshold": Decimal("0.75"),
    },
    # ── 价格类（2）───────────────────────────────────────────────────────
    {
        "kpi_code": "KPI_PURCHASE_PRICE_VARIANCE",
        "semantic_keywords": [
            "价格偏差",
            "价格差异",
            "price variance",
            "采购价格",
            "价格波动",
            "price deviation",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_COST_SAVING",
        "semantic_keywords": [
            "成本节约",
            "cost saving",
            "节支",
            "成本节省",
            "采购节支",
            "cost reduction",
        ],
        "match_threshold": Decimal("0.75"),
    },
    # ── 财务/合规类（3）───────────────────────────────────────────────────
    {
        "kpi_code": "KPI_INVOICE_MATCH_RATE",
        "semantic_keywords": [
            "三单匹配",
            "发票匹配",
            "invoice match",
            "票货核对",
            "invoice reconciliation",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_PAYMENT_ON_TIME",
        "semantic_keywords": [
            "付款及时率",
            "按时付款",
            "payment on time",
            "付款率",
            "账期",
            "准时付款",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_MAINTENANCE_RATIO",
        "semantic_keywords": [
            "资质合规",
            "合规率",
            "certificate",
            "资质证书",
            "供应商资质",
            "compliance",
        ],
        "match_threshold": Decimal("0.75"),
    },
]


async def _publishedSpecs(session: Any) -> list[tuple[KpiCatalog, dict[str, Any]]]:
    """取「存在且 PUBLISHED」的 KPI 及其关键词规格（DRAFT/DEPRECATED 不碰）。"""
    pairs: list[tuple[KpiCatalog, dict[str, Any]]] = []
    for spec in KPI_SEMANTIC_INDEX:
        row = (
            await session.execute(
                select(KpiCatalog).where(KpiCatalog.kpi_code == spec["kpi_code"])
            )
        ).scalar_one_or_none()
        if row is None or row.status != KpiStatus.PUBLISHED.value:
            continue
        pairs.append((row, spec))
    return pairs


async def previewKpiSemanticIndex(session: Any) -> tuple[str, ...]:
    """``--dry-run``：列出将更新的 KPI 与字段（不写库）。"""
    lines: list[str] = []
    for row, spec in await _publishedSpecs(session):
        keywords = "、".join(spec["semantic_keywords"])
        lines.append(
            f"- {row.kpi_code}：semantic_keywords=[{keywords}] "
            f"match_threshold={spec['match_threshold']}"
        )
    return tuple(lines)


async def seedKpiSemanticIndex(session: Any) -> int:
    """幂等写入 12 条 KPI 的 semantic_keywords；返回 affected 行数。

    **改走 `KpiCatalogService.updateKpi`**（修 C3）。原来的
    `sqlalchemy.update(...)` 是 Core UPDATE，绕过 service 链路 ⇒ 少了：
    - 审计与追踪：audit_log / kpi_catalog_history / outbox 事件；
    - 写后刷新：`updateKpi` 内部的 `refreshOne` 把该 KPI 重新写进匹配索引；
    - 统一校验：关键词与阈值归一（`match_threshold` 在本层转 `Decimal`）。

    仍只更新 status=PUBLISHED 的 KPI（DRAFT/DEPRECATED 不碰）。重复运行覆盖旧值。

    闸门说明（T15-K）：`updateKpi` 只在本次更新带 `status=PUBLISHED` 时才走
    发布闸门；本脚本只改关键词/阈值 ⇒ 不会触发 409，因此**不需要**捕获
    `ConflictError`（该分支永远不可能执行，已删除）。
    """
    service = KpiCatalogService()
    actor = CurrentUser(userId="system-seed", roles=(ADMIN_ROLE,), departments=())
    affected = 0
    for row, spec in await _publishedSpecs(session):
        await service.updateKpi(
            session,
            row.id,
            KpiCatalogUpdate(
                semantic_keywords=list(spec["semantic_keywords"]),
                match_threshold=float(spec["match_threshold"]),
            ),
            actor,
        )
        affected += 1
    return affected


def _parseArgs(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KPI 语义关键词种子（默认写库；--dry-run 只预览）"
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="目标库 DSN（必选；不读任何环境变量）",
    )
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help="允许目标是正式库 qa_metadata（写前自动备份）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将更新的 KPI，不写库")
    return parser.parse_args(list(argv) if argv is not None else None)


def _requireDatabaseUrl(explicit: str | None) -> str:
    """闸 1：只认 ``--database-url``，**不读** ``DATABASE_URL`` / ``TEST_DATABASE_URL``。

    dev 工作流普遍导出 ``DATABASE_URL`` ⇒ 作为兜底会把种子打到非预期库；
    本仓其它工具链（``kpi_metric_realdata_lib.resolveDatabaseUrl``）同口径。
    """
    if not explicit:
        raise SystemExit(
            "缺少数据库连接：请用 --database-url 指定目标库。"
            "本脚本不猜环境变量（不读 DATABASE_URL / TEST_DATABASE_URL —— "
            "本机默认配置指向 5432 的 OpenMetadata PG，不可信）。"
        )
    return explicit


async def _runSeed(args: argparse.Namespace) -> int:
    databaseUrl = _requireDatabaseUrl(args.database_url)
    targetDb = assertSafeTarget(databaseUrl, allowProdWrite=args.allow_prod_write)
    if not args.dry_run and targetDb == PROD_DB_NAME:
        revision = await _alembicRevision(databaseUrl)
        _printBanner(targetDb, _ensureProdBackup(databaseUrl, revision))

    print(f"目标库：{targetDb}（{maskDsn(databaseUrl)}）")
    factory = buildSessionFactory(databaseUrl)
    try:
        async with factory() as session:
            await _assertSameDatabase(session, targetDb)
            if args.dry_run:
                pending = await previewKpiSemanticIndex(session)
                print("预览模式：未写库。将更新以下 KPI：")
                print("\n".join(pending) if pending else "（无可更新项）")
                return 0
            affected = await seedKpiSemanticIndex(session)
    except (OperationalError, OSError, DBAPIError, asyncpg.PostgresError) as exc:
        raise connectionErrorExit(exc, databaseUrl) from exc
    finally:
        await factory.kw["bind"].dispose()
    print(
        f"[seed_kpi_semantic_index] affected={affected} 条 "
        f"（12 KPI semantic index，Task 1.5 验收）"
    )
    print("✅ KPI semantic index 种子完成（12 条：4 交付 + 3 质量 + 2 价格 + 3 财务/合规）")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_runSeed(_parseArgs(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
