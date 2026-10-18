"""P0 回填脚本集成测试（真实 PG；脚本按路径 importlib 加载）。

覆盖：
- CSV 解析（表头校验 / 行号 / 空行跳过）
- 预览只读：未绑定 KPI 带建议、库不变
- 落库：名称/ID 两种写法，无法解析的数据源 → failed；二次执行全部 skipped（幂等）
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, KpiCatalog

_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/
_SCRIPT = _BACKEND_ROOT / "scripts" / "backfill_kpi_datasource.py"


def _loadModule():
    spec = importlib.util.spec_from_file_location("backfill_kpi_datasource", str(_SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 需要模块在 sys.modules 中
    spec.loader.exec_module(module)
    return module


async def _addSource(dbSession: AsyncSession, name: str) -> DataSource:
    ds = DataSource(
        name=name,
        type="mysql",
        host="x",
        port=3306,
        database_name="x",
        username="x",
        password_encrypted="x",
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def _addKpi(
    dbSession: AsyncSession,
    code: str,
    *,
    legacyName: str | None = None,
    datasourceId: int | None = None,
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=code,
        version="1",
        semantic_keywords=[],
        match_threshold=Decimal("0.5"),
        data_source=legacyName,
        datasource_id=datasourceId,
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


def test_parse_csv_skips_blank_lines(tmp_path: Path) -> None:
    module = _loadModule()
    path = tmp_path / "m.csv"
    path.write_text("kpi_code,datasource\nK1,DWS_A\n\nK2,3\n", encoding="utf-8")

    rows = module.parseMappingCsv(path)

    assert [(r.row, r.kpi_code, r.datasource) for r in rows] == [
        (2, "K1", "DWS_A"),
        (4, "K2", "3"),
    ]


def test_parse_csv_rejects_bad_header(tmp_path: Path) -> None:
    module = _loadModule()
    path = tmp_path / "m.csv"
    path.write_text("code,ds\nK1,DWS_A\n", encoding="utf-8")

    with pytest.raises(ValueError, match="表头"):
        module.parseMappingCsv(path)


@pytest.mark.asyncio
async def test_preview_is_read_only_and_includes_suggestion(
    dbSession: AsyncSession,
) -> None:
    module = _loadModule()
    ds = await _addSource(dbSession, "BACKFILL_PREVIEW_DS")
    kpi = await _addKpi(dbSession, "BACKFILL_KPI_PREVIEW", legacyName="backfill_preview_ds")

    rows = await module.buildPreview(dbSession)

    hit = next(r for r in rows if r.kpi_code == "BACKFILL_KPI_PREVIEW")
    assert hit.suggestion_name == "BACKFILL_PREVIEW_DS"
    assert hit.suggestion_reason == "legacy_name_exact"
    stored = (
        await dbSession.execute(
            select(KpiCatalog)
            .where(KpiCatalog.id == kpi.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert stored.datasource_id is None
    assert ds.id > 0


@pytest.mark.asyncio
async def test_run_backfill_resolves_names_and_ids_then_is_idempotent(
    dbSession: AsyncSession, tmp_path: Path,
) -> None:
    module = _loadModule()
    ds = await _addSource(dbSession, "BACKFILL_TARGET_DS")
    kpiByName = await _addKpi(dbSession, "BACKFILL_KPI_BY_NAME")
    kpiById = await _addKpi(dbSession, "BACKFILL_KPI_BY_ID")
    kpiMissing = await _addKpi(dbSession, "BACKFILL_KPI_MISSING_DS")

    path = tmp_path / "m.csv"
    path.write_text(
        "kpi_code,datasource\n"
        "BACKFILL_KPI_BY_NAME,BACKFILL_TARGET_DS\n"
        f"BACKFILL_KPI_BY_ID,{ds.id}\n"
        "BACKFILL_KPI_MISSING_DS,NO_SUCH_DS\n",
        encoding="utf-8",
    )
    rows = module.parseMappingCsv(path)

    results, bound, skipped, failed = await module.runBackfill(
        dbSession, rows, force=False
    )

    assert (bound, skipped, failed) == (2, 0, 1)
    unresolved = next(r for r in results if r["kpiCode"] == "BACKFILL_KPI_MISSING_DS")
    assert unresolved["status"] == "failed"
    assert "NO_SUCH_DS" in unresolved["error"]
    for kpi in (kpiByName, kpiById):
        refreshed = (
            await dbSession.execute(
                select(KpiCatalog)
                .where(KpiCatalog.id == kpi.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert refreshed.datasource_id == ds.id
    assert (
        await dbSession.execute(
            select(KpiCatalog)
            .where(KpiCatalog.id == kpiMissing.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one().datasource_id is None

    # 幂等：二次执行已绑定行 skipped，不再产生 revision
    _, bound2, skipped2, _ = await module.runBackfill(dbSession, rows, force=False)
    assert (bound2, skipped2) == (0, 2)
