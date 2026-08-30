"""Phase 4.2 KPI Catalog 种子脚本测试（真实 PG 5433）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL + 完整链路，禁止 sqlite 内存库。

覆盖：
1. seedKpiCatalog() 插入 12 条核心 KPI
2. 幂等性：第二次调用不产生重复（INSERT ... ON CONFLICT DO NOTHING）
3. 12 条 KPI 字段完整：每个 kpi_code 都至少有 kpi_name / owner / unit / status
4. 覆盖 Sheet 13 全部 4 类（交付/质量/价格/财务/合规）
5. KPI_SUPPLIER_OTD 与 KPI_PURCHASE_PRICE_VARIANCE 必含（采购域 §六 重点示例）
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from sqlalchemy import delete, func, select

from app.domain.models import KpiCatalog


_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_SEED_SCRIPT = _BACKEND_ROOT / "scripts" / "seed_kpi_catalog.py"


def _loadSeedModule():
    """按文件路径加载（与 check_schema_drift 测试同模式，避免 pytest sys.path 副作用）。"""
    spec = importlib.util.spec_from_file_location(
        "seed_kpi_catalog", str(_SEED_SCRIPT)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSeedKpiCatalog:
    """种子脚本正确性 + 幂等性 + 字段完整性。"""

    async def _cleanKpiCatalog(self, dbSession) -> None:
        """每个测试前清空 kpi_catalog 中 seed 行（仅 KPI_SEEDS 范围），保证隔离且不影响其他测试可能插入的非种子行。"""
        seed_codes = [k["kpi_code"] for k in _loadSeedModule().KPI_SEEDS]
        await dbSession.execute(
            delete(KpiCatalog).where(KpiCatalog.kpi_code.in_(seed_codes))
        )
        await dbSession.commit()

    async def test_seed_inserts_twelve_kpis(self, dbSession) -> None:
        """首次运行插入 12 条。"""
        await self._cleanKpiCatalog(dbSession)
        seed = _loadSeedModule()
        inserted = await seed.seedKpiCatalog(dbSession)
        assert inserted == 12, f"应新增 12 条，实际 {inserted}"

        total = (
            await dbSession.execute(select(func.count()).select_from(KpiCatalog))
        ).scalar()
        assert total == 12

    async def test_seed_is_idempotent(self, dbSession) -> None:
        """第二次运行 0 新增；总条数仍为 12。"""
        await self._cleanKpiCatalog(dbSession)
        seed = _loadSeedModule()
        first = await seed.seedKpiCatalog(dbSession)
        second = await seed.seedKpiCatalog(dbSession)
        assert first == 12
        assert second == 0, f"第二次应不新增，实际 {second}"

        total = (
            await dbSession.execute(select(func.count()).select_from(KpiCatalog))
        ).scalar()
        assert total == 12

    async def test_all_seeds_have_required_fields(self, dbSession) -> None:
        """每个 KPI 至少有 kpi_code / kpi_name / status / owner。"""
        await self._cleanKpiCatalog(dbSession)
        seed = _loadSeedModule()
        await seed.seedKpiCatalog(dbSession)

        result = await dbSession.execute(select(KpiCatalog))
        rows = list(result.scalars().all())
        assert len(rows) == 12
        for r in rows:
            assert r.kpi_code and r.kpi_code.startswith("KPI_"), r.kpi_code
            assert r.kpi_name, f"{r.kpi_code} kpi_name 为空"
            assert r.status in {"DRAFT", "PUBLISHED", "DEPRECATED"}
            assert r.owner, f"{r.kpi_code} owner 为空"
            assert r.unit, f"{r.kpi_code} unit 为空"
            assert r.grain, f"{r.kpi_code} grain 为空"
            assert r.version == "v1.0"

    async def test_sheet13_categories_covered(self, dbSession) -> None:
        """覆盖 Sheet 13 全部 4 类：交付/质量/价格/财务。"""
        await self._cleanKpiCatalog(dbSession)
        seed = _loadSeedModule()
        await seed.seedKpiCatalog(dbSession)

        result = await dbSession.execute(select(KpiCatalog.kpi_code))
        codes = {row[0] for row in result}

        # 交付类（4）
        assert "KPI_SUPPLIER_OTD" in codes
        assert "KPI_SUPPLIER_OVERDUE_RATIO" in codes
        assert "KPI_PURCHASE_CYCLE_TIME" in codes
        assert "KPI_SUPPLIER_DELAY_DAYS" in codes
        # 质量类（3）
        assert "KPI_SUPPLIER_DEFECT_RATE" in codes
        assert "KPI_SUPPLIER_FPY" in codes
        assert "KPI_SUPPLIER_NCR_RATE" in codes
        # 价格类（2）
        assert "KPI_PURCHASE_PRICE_VARIANCE" in codes
        assert "KPI_COST_SAVING" in codes
        # 财务/合规类（3）
        assert "KPI_INVOICE_MATCH_RATE" in codes
        assert "KPI_PAYMENT_ON_TIME" in codes
        assert "KPI_SUPPLIER_MAINTENANCE_RATIO" in codes

    async def test_procurement_doc_required_kpis_present(self, dbSession) -> None:
        """采购域 §六 Sheet 13 重点示例（KPI_SUPPLIER_OTD + KPI_PURCHASE_PRICE_VARIANCE）。"""
        await self._cleanKpiCatalog(dbSession)
        seed = _loadSeedModule()
        await seed.seedKpiCatalog(dbSession)

        otd = (
            await dbSession.execute(
                select(KpiCatalog).where(KpiCatalog.kpi_code == "KPI_SUPPLIER_OTD")
            )
        ).scalar_one()
        assert otd.unit == "%"
        assert "准时" in otd.kpi_name
        assert otd.status == "PUBLISHED"

        price_var = (
            await dbSession.execute(
                select(KpiCatalog).where(
                    KpiCatalog.kpi_code == "KPI_PURCHASE_PRICE_VARIANCE"
                )
            )
        ).scalar_one()
        assert price_var.unit == "%"
        assert price_var.status == "PUBLISHED"