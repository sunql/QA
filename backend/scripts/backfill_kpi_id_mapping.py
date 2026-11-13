"""存量 KPI 目录 id_mapping 回填（幂等，feat-kpi-vector-graph-sync §3.3）。

KPI 此前从未注册 unified_id；向量/图同步需要 obj:KPI:{id}。本脚本为全部
kpi_catalog 行（含 DRAFT/DEPRECATED —— id_mapping 是身份层，全量注册；
向量索引才按 PUBLISHED 过滤，见 kpi_vector_graph_sync）补注册 id_mapping。

写库闸门（三道，缺一不可）
--------------------------
1. **库名白名单**：目标库必须在 ``qa_metadata`` / ``qa_metadata_test*`` 内
   （复用 ``scripts.kpi_metric_db_guard.assertSafeTarget`` —— 本仓只有一份正则）。
2. **正式库显式 opt-in**：目标库名是 ``qa_metadata``（**dev 与 prod 同名**）时，
   必须加 ``--allow-prod-write``。
3. **APP_ENV 判定**：``APP_ENV`` 是 production/prod 时，同样必须
   ``--allow-prod-write`` —— 白名单挡不住同名 dev/prod，这道才是真闸。

⚠️ 库名白名单挡不住「dev 5433 与 prod 同名」⇒ **dev 5433 的写同样需要
``--allow-prod-write``**，即「无批准不写」。

用法
----
    # 预演（只读）
    uv run python scripts/backfill_kpi_id_mapping.py --database-url '<DSN>'
    # 落库
    uv run python scripts/backfill_kpi_id_mapping.py --apply --database-url '<DSN>'
    # 目标库名是 qa_metadata（dev 5433 或生产）⇒ 额外加 --allow-prod-write
    uv run python scripts/backfill_kpi_id_mapping.py --apply --allow-prod-write \\
        --database-url '<DSN>'

DSN 来源：``--database-url`` 优先，否则读环境变量 ``DATABASE_URL``；
**不读** ``TEST_DATABASE_URL``（防止误打测试库）。

⚠️ 本脚本只 INSERT id_mapping、且逐行幂等（已存在即跳过）⇒ 无需 pg_dump 备份；
生产发版仍须按 runbook 先做当日备份。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.models import KpiCatalog
from app.domain.schemas import IdMappingCreate
from app.services.id_mapping_service import IdMappingService
from app.services.kpi_vector_graph_sync import KPI_BUSINESS_OBJECT, KPI_PG_TABLE
from scripts.kpi_metric_db_guard import assertSafeTarget, maskDsn

__all__ = ["backfillKpiIdMappings", "buildSessionFactory", "main"]

_PRODUCTION_APP_ENVS = frozenset({"production", "prod"})


def buildSessionFactory(databaseUrl: str) -> async_sessionmaker[AsyncSession]:
    """按**守卫放行的那个 DSN** 建会话工厂（DSN 单一来源，勿用 app 全局工厂）。"""
    engine = create_async_engine(databaseUrl, echo=False, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _registerKpi(
    idMappingSvc: IdMappingService, session: AsyncSession, kpiId: int
) -> str:
    """注册单行 KPI 的 id_mapping，返回 unified_id。失败抛给调用方处置。"""
    mapping = await idMappingSvc.register(
        session,
        IdMappingCreate(
            business_object=KPI_BUSINESS_OBJECT,
            external_id=str(kpiId),
        ),
        pg_table=KPI_PG_TABLE,
        pg_id=str(kpiId),
    )
    return mapping.unified_id


async def _snapshotKpiRefs(session: AsyncSession) -> list[tuple[int, str]]:
    """一次性把 kpi_catalog 读成**普通值**列表 ``(id, kpi_code)``。

    ⚠️ **循环体外取快照**（不是循环体内）：``IdMappingService.register`` 在
    IntegrityError 时会 ``await session.rollback()`` ⇒ session identity map 里
    所有持久化实例的属性**全部过期**。若在循环体内读 ``kpi.id``，那么上一轮
    rollback 之后的那一行会触发 async 下的**同步懒加载** ⇒ ``MissingGreenlet``，
    且该异常发生在下一轮循环开头（不在 try 保护范围内）⇒ 整批回填带 traceback
    崩掉，而不是「计入 failed 后继续」。

    故：这里把属性读成 int/str 快照，循环体只遍历普通值，不再触碰 ORM 实例。
    """
    rows = (await session.execute(select(KpiCatalog))).scalars().all()
    return [(int(kpi.id), str(kpi.kpi_code)) for kpi in rows]


async def backfillKpiIdMappings(session: AsyncSession, apply: bool) -> dict:
    """幂等回填：已注册跳过，未注册补 register。返回摘要 dict。"""
    idMappingSvc = IdMappingService()
    kpiRefs = await _snapshotKpiRefs(session)
    registered = 0
    alreadyMapped = 0
    failed = 0
    failures: list[dict] = []
    for kpiId, kpiCode in kpiRefs:
        existing = await idMappingSvc.resolveByExternal(
            session, KPI_BUSINESS_OBJECT, str(kpiId)
        )
        if existing is not None:
            alreadyMapped += 1
            continue
        if not apply:
            print(f"  [dry-run] 将注册 id={kpiId} code={kpiCode}")
            continue
        try:
            unifiedId = await _registerKpi(idMappingSvc, session, kpiId)
            registered += 1
            print(f"  ok id={kpiId} code={kpiCode} uid={unifiedId}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failures.append({"kpiId": kpiId, "error": str(exc)})
            print(f"  FAIL id={kpiId} code={kpiCode}: {exc}")
    summary = {
        "totalKpis": len(kpiRefs),
        "alreadyMapped": alreadyMapped,
        "registered": registered,
        "failed": failed,
        "failures": failures,
    }
    print(
        f"KPI id_mapping 回填: 共 {summary['totalKpis']}，"
        f"已存在 {alreadyMapped}，新注册 {registered}，失败 {failed}"
    )
    return summary


def _resolveDatabaseUrl(explicit: str | None) -> str:
    """`--database-url` 优先；否则读 `DATABASE_URL`；都没有 ⇒ 拒绝（不猜）。"""
    dsn = (explicit or os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        raise SystemExit(
            "拒绝执行：未提供目标库 DSN。用 --database-url '<DSN>' 或设置 "
            "DATABASE_URL 环境变量（不读 TEST_DATABASE_URL）。"
        )
    return dsn


def assertSafeWriteTarget(
    *, appEnv: str, databaseUrl: str, allowProdWrite: bool
) -> str:
    """三道闸：库名白名单 → 正式库 opt-in → APP_ENV 判定。拒绝时 `SystemExit`。"""
    targetDb = assertSafeTarget(databaseUrl, allowProdWrite=allowProdWrite)
    if (appEnv or "").strip().lower() in _PRODUCTION_APP_ENVS and not allowProdWrite:
        raise SystemExit(
            f"拒绝执行：APP_ENV={appEnv!r} 判定为生产环境。"
            "确认后加 --allow-prod-write（并先完成当日备份）。"
        )
    return targetDb


async def _assertSameDatabase(session: AsyncSession, allowedDbName: str) -> None:
    """守卫放行的库名必须等于会话**真正连的**库。"""
    actual = (await session.execute(text("SELECT current_database()"))).scalar_one()
    if actual != allowedDbName:
        raise RuntimeError(
            f"落库目标与守卫放行目标不一致：{actual!r} != {allowedDbName!r}"
        )


async def _run(args: argparse.Namespace) -> int:
    databaseUrl = _resolveDatabaseUrl(args.database_url)
    targetDb = assertSafeWriteTarget(
        appEnv=os.environ.get("APP_ENV", "development"),
        databaseUrl=databaseUrl,
        allowProdWrite=args.allow_prod_write,
    )
    print(f"目标库：{targetDb}（{maskDsn(databaseUrl)}）")
    factory = buildSessionFactory(databaseUrl)
    try:
        async with factory() as session:
            await _assertSameDatabase(session, targetDb)
            summary = await backfillKpiIdMappings(session, apply=args.apply)
            if not args.apply:
                print("预演模式：未写库。确认无误后加 `--apply` 落库。")
            return 1 if summary["failed"] else 0
    finally:
        await factory.kw["bind"].dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="实际写入（缺省为预演）")
    mode.add_argument("--dry-run", action="store_true", help="显式预演（缺省行为）")
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help="目标库名是 qa_metadata（dev 5433 与生产同名）时必须加",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="目标库 DSN（优先于环境变量 DATABASE_URL；不读 TEST_DATABASE_URL）",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
