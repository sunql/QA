"""存量 KPI id_mapping 回填：真 PG，幂等（重复跑 registered=0）。

覆盖两层：
- **函数层** ``backfillKpiIdMappings`` —— 幂等注册 + 失败计入 failures；
- **CLI 层** ``main`` / ``_run`` / ``buildSessionFactory`` —— 三道写库闸门全在
  这一层，只测函数等于没测闸。

⚠️ CLI 用**自己建**的 engine（DSN 单一来源），与测试夹具的 engine 是两个连接，
但**同一个库**（TEST_DATABASE_URL）⇒ 断言可跨连接观察。
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete as saDelete
from sqlalchemy import func as saFunc
from sqlalchemy import select, text

from app.domain.models import IdMapping, KpiCatalog
from app.services.id_mapping_service import IdMappingService
from scripts.backfill_kpi_id_mapping import (
    _assertSameDatabase,
    _resolveDatabaseUrl,
    assertSafeWriteTarget,
    backfillKpiIdMappings,
    buildSessionFactory,
    main,
)

pytestmark = pytest.mark.asyncio

_TEST_CODES = ("KPI_BF_TEST_A", "KPI_BF_TEST_B")
_CLI_CODE = "KPI_CLI_TEST_001"

# 闸门测试用的假 DSN（**不连接**，只解析库名）
_GUARD_TEST_DSN = "postgresql+asyncpg://qa_user:pwd@127.0.0.1:5434/qa_metadata_test"
# 库名是 qa_metadata（dev 5433 与 prod 同名）⇒ 守卫要求 --allow-prod-write。
_NAMED_PROD_DSN = "postgresql+asyncpg://qa_user:pwd@127.0.0.1:5433/qa_metadata"
_OTHER_DSN = "postgresql+asyncpg://qa_user:pwd@127.0.0.1:5432/some_other_db"

# 真 DSN（CLI 真跑时连接）：必须是隔离测试库。
_REAL_TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")


async def _cleanup(session) -> None:
    """两张表都清 —— 只清 id_mapping 会撞 uq_kpi_catalog_code。"""
    await session.execute(
        saDelete(IdMapping).where(
            IdMapping.business_object == "KPI",
            IdMapping.pg_table == "kpi_catalog",
        )
    )
    await session.execute(
        saDelete(KpiCatalog).where(KpiCatalog.kpi_code.in_(_TEST_CODES))
    )
    await session.commit()


async def _cleanupCli(session) -> None:
    """CLI 用例的清理（CLI 会给库里**所有** KPI 行补 id_mapping）。"""
    await session.execute(
        saDelete(IdMapping).where(IdMapping.business_object == "KPI")
    )
    await session.execute(
        saDelete(KpiCatalog).where(KpiCatalog.kpi_code == _CLI_CODE)
    )
    await session.commit()


@pytest_asyncio.fixture
async def seedKpis(dbSession):
    """真 PG 种子（async fixture —— 同步 fixture 里的 await 不会被执行）。"""
    made: list[KpiCatalog] = []
    for code in _TEST_CODES:
        kpi = KpiCatalog(
            kpi_code=code,
            kpi_name=f"回填 {code}",
            status="PUBLISHED",
            match_threshold=Decimal("0.75"),
        )
        dbSession.add(kpi)
        made.append(kpi)
    await dbSession.commit()
    for kpi in made:
        await dbSession.refresh(kpi)
    return made


async def _seedCliKpi(session) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=_CLI_CODE,
        kpi_name="CLI 回填测试",
        status="PUBLISHED",
        match_threshold=Decimal("0.75"),
    )
    session.add(kpi)
    await session.commit()
    await session.refresh(kpi)
    return kpi


async def test_backfill_registers_and_is_idempotent(seedKpis, dbSession):
    try:
        first = await backfillKpiIdMappings(dbSession, apply=True)
        assert first["registered"] >= len(seedKpis)
        assert first["failed"] == 0
        second = await backfillKpiIdMappings(dbSession, apply=True)
        # 幂等：复跑不再注册任何新行
        assert second["registered"] == 0
        assert second["alreadyMapped"] >= len(seedKpis)
        row = await IdMappingService().resolveByExternal(
            dbSession, "KPI", str(seedKpis[0].id)
        )
        assert row is not None
        assert row.unified_id == f"obj:KPI:{seedKpis[0].id}"
    finally:
        await _cleanup(dbSession)


# ---------------------------------------------------------------------------
# 写库闸门（三道）—— 安全关键，逐道双向断言
# ---------------------------------------------------------------------------


async def test_guard_rejects_database_outside_whitelist():
    """库名白名单：不在 qa_metadata / qa_metadata_test* 内 ⇒ 拒绝。"""
    with pytest.raises(SystemExit):
        assertSafeWriteTarget(
            appEnv="development", databaseUrl=_OTHER_DSN, allowProdWrite=False
        )


async def test_guard_requires_optin_for_named_prod_database():
    """库名是 qa_metadata（dev 5433 与 prod 同名）⇒ 必须显式 opt-in。"""
    with pytest.raises(SystemExit):
        assertSafeWriteTarget(
            appEnv="development", databaseUrl=_NAMED_PROD_DSN, allowProdWrite=False
        )


async def test_guard_rejects_production_app_env_even_for_test_database():
    """APP_ENV=production ⇒ 即便库名像测试库也必须显式 opt-in。"""
    with pytest.raises(SystemExit):
        assertSafeWriteTarget(
            appEnv="production", databaseUrl=_GUARD_TEST_DSN, allowProdWrite=False
        )


async def test_guard_allows_test_database_without_optin():
    assert (
        assertSafeWriteTarget(
            appEnv="development", databaseUrl=_GUARD_TEST_DSN, allowProdWrite=False
        )
        == "qa_metadata_test"
    )


async def test_resolve_database_url_prefers_explicit_argument(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _NAMED_PROD_DSN)
    assert _resolveDatabaseUrl(_GUARD_TEST_DSN) == _GUARD_TEST_DSN


async def test_resolve_database_url_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _NAMED_PROD_DSN)
    assert _resolveDatabaseUrl(None) == _NAMED_PROD_DSN


async def test_resolve_database_url_rejects_missing_dsn(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        _resolveDatabaseUrl(None)


async def test_assert_same_database_rejects_mismatch(dbSession):
    with pytest.raises(RuntimeError):
        await _assertSameDatabase(dbSession, "definitely_not_the_current_db")


async def test_dry_run_does_not_write_any_row(seedKpis, dbSession):
    """预演模式：registered=0 且 id_mapping 一行都不写。"""
    try:
        summary = await backfillKpiIdMappings(dbSession, apply=False)
        assert summary["registered"] == 0
        assert summary["alreadyMapped"] == 0
        assert summary["totalKpis"] >= len(seedKpis)
        count = (
            await dbSession.execute(
                select(saFunc.count())
                .select_from(IdMapping)
                .where(IdMapping.business_object == "KPI")
            )
        ).scalar_one()
        assert count == 0
    finally:
        await _cleanup(dbSession)


async def test_register_failure_is_reported_not_raised(seedKpis, dbSession, monkeypatch):
    """单行注册失败 ⇒ 计入 failed/failures，不中断整批。"""
    from app.services import id_mapping_service as idMappingModule

    async def _boom(self, session, dto, **kwargs):
        raise RuntimeError("模拟注册失败")

    monkeypatch.setattr(idMappingModule.IdMappingService, "register", _boom)
    try:
        summary = await backfillKpiIdMappings(dbSession, apply=True)
        assert summary["failed"] >= 1
        assert summary["registered"] == 0
        assert summary["failures"]
        first = summary["failures"][0]
        assert set(first.keys()) == {"kpiId", "error"}
        assert "模拟注册失败" in first["error"]
    finally:
        await _cleanup(dbSession)


# ---------------------------------------------------------------------------
# CLI 层（真跑脚本入口）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanAppEnv(monkeypatch):
    """APP_ENV 不受外部环境影响（否则 production 判定会让 CLI 恒被拦）。"""
    monkeypatch.delenv("APP_ENV", raising=False)


def _requireRealTestDsn() -> str:
    """CLI 用例要真连库 ⇒ DSN 必须是隔离测试库（点错库等于写生产）。"""
    if not _REAL_TEST_DSN:
        pytest.skip("需要 TEST_DATABASE_URL")
    from app.tests._pg_support import assertTestDatabase

    assertTestDatabase(_REAL_TEST_DSN)
    return _REAL_TEST_DSN


async def test_cli_dry_run_writes_nothing(dbSession):
    dsn = _requireRealTestDsn()
    kpi = await _seedCliKpi(dbSession)
    try:
        exitCode = await asyncio.to_thread(
            main, ["--dry-run", "--database-url", dsn]
        )
        assert exitCode == 0
        count = (
            await dbSession.execute(
                select(saFunc.count())
                .select_from(IdMapping)
                .where(
                    IdMapping.business_object == "KPI",
                    IdMapping.external_id == str(kpi.id),
                )
            )
        ).scalar_one()
        assert count == 0
    finally:
        await _cleanupCli(dbSession)


async def test_cli_apply_writes_id_mapping(dbSession):
    dsn = _requireRealTestDsn()
    kpi = await _seedCliKpi(dbSession)
    try:
        exitCode = await asyncio.to_thread(
            main, ["--apply", "--database-url", dsn]
        )
        assert exitCode == 0
        row = (
            await dbSession.execute(
                select(IdMapping).where(
                    IdMapping.business_object == "KPI",
                    IdMapping.external_id == str(kpi.id),
                )
            )
        ).scalar_one()
        assert row.unified_id == f"obj:KPI:{kpi.id}"
        assert row.pg_table == "kpi_catalog"
    finally:
        await _cleanupCli(dbSession)


async def test_cli_rejects_named_prod_database_without_optin():
    """库名 qa_metadata 且无 --allow-prod-write ⇒ 拒绝（拦在连库之前）。"""
    with pytest.raises(SystemExit):
        await asyncio.to_thread(
            main, ["--apply", "--database-url", _NAMED_PROD_DSN]
        )


async def test_cli_rejects_missing_dsn(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        await asyncio.to_thread(main, ["--dry-run"])


async def test_build_session_factory_targets_the_same_database():
    """会话工厂连的库必须就是 DSN 里那个库（DSN 单一来源）。"""
    dsn = _requireRealTestDsn()
    factory = buildSessionFactory(dsn)
    try:
        async with factory() as session:
            actual = (
                await session.execute(text("SELECT current_database()"))
            ).scalar_one()
        assert actual == "qa_metadata_test"
    finally:
        await factory.kw["bind"].dispose()
