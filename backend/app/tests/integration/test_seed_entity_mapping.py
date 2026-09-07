"""Phase 3.2 entity_mapping 种子脚本集成测试（真实 PG 5433）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL + 完整链路，
禁止 sqlite 内存库。dbSession / client fixtures 走 _pg_support.pgApiClient()，
每测试 TRUNCATE 隔离。

覆盖 `scripts/seed_entity_mapping.py::seedEntityMappings` 的契约：
1. 首次运行插入全部 45 条（25 供应商 + 15 物料 + 3 PO + 2 GR/IQC）
2. 再次运行幂等（按唯一键 (entity_type, enterprise_key, source_system) 查重，新增 0 条）
3. 按实体类型分布正确
4. 覆盖 ERP / SRM / QMS 三源系统（跨系统追溯验收）
5. PO/GR/IQC 用 BUSINESS_KEY 匹配规则；SUPPLIER/MATERIAL 用 MDM_MASTER
6. 种子数据可通过 HTTP API 列表查询到（完整 API 链路）
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from app.domain.enums import MatchRule, SourceSystem
from app.domain.models import EntityMapping
from scripts.seed_entity_mapping import seedEntityMappings

# 种子默认有效期（与脚本 _DEFAULT_EFFECTIVE 对齐）
EXPECTED_EFFECTIVE = date(2026, 1, 1)

EXPECTED_TOTAL = 45
EXPECTED_BY_TYPE: dict[str, int] = {
    "SUPPLIER": 25,
    "MATERIAL": 15,
    "PO": 3,
    "GR": 1,
    "IQC": 1,
}
# 跨系统追溯验收：至少覆盖 ERP / SRM / QMS 三系统
EXPECTED_SYSTEMS = {SourceSystem.ERP, SourceSystem.SRM, SourceSystem.QMS}
# 业务键匹配：PO(3) + GR(1) + IQC(1) = 5
EXPECTED_BUSINESS_KEY_COUNT = 5
# MDM 主数据匹配：SUPPLIER(25) + MATERIAL(15) = 40
EXPECTED_MDM_MASTER_COUNT = 40


class TestSeedEntityMapping:
    async def _count_rows(self, dbSession) -> int:
        return (
            await dbSession.execute(select(func.count()).select_from(EntityMapping))
        ).scalar()

    async def test_first_run_inserts_all_mappings(self, dbSession) -> None:
        inserted = await seedEntityMappings(dbSession)
        assert inserted == EXPECTED_TOTAL
        assert await self._count_rows(dbSession) == EXPECTED_TOTAL

    async def test_second_run_is_idempotent(self, dbSession) -> None:
        await seedEntityMappings(dbSession)
        inserted = await seedEntityMappings(dbSession)
        assert inserted == 0
        # 显式断言总数不变（不只依赖返回值的 0）
        assert await self._count_rows(dbSession) == EXPECTED_TOTAL

    async def test_partial_pre_existing_state_inserts_only_missing(self, dbSession) -> None:
        """预置 1 条与种子重叠的映射 → seed 只补 44 条，总数保持 45。"""
        dbSession.add(
            EntityMapping(
                entity_type="SUPPLIER",
                enterprise_key=100001,
                enterprise_code="SUP000001",
                source_system=SourceSystem.ERP,
                source_key="V000001",
                source_code="V000001",
                match_rule=MatchRule.MDM_MASTER,
                effective_date=EXPECTED_EFFECTIVE,
                expiry_date=None,
            )
        )
        await dbSession.commit()
        inserted = await seedEntityMappings(dbSession)
        assert inserted == EXPECTED_TOTAL - 1
        assert await self._count_rows(dbSession) == EXPECTED_TOTAL

    async def test_row_counts_by_entity_type(self, dbSession) -> None:
        await seedEntityMappings(dbSession)
        for entity_type, expected in EXPECTED_BY_TYPE.items():
            count = (
                await dbSession.execute(
                    select(func.count()).select_from(EntityMapping).where(
                        EntityMapping.entity_type == entity_type
                    )
                )
            ).scalar()
            assert count == expected, f"{entity_type} 应有 {expected} 条，实际 {count}"

    async def test_covers_erp_srm_qms_systems(self, dbSession) -> None:
        await seedEntityMappings(dbSession)
        systems = set(
            (await dbSession.execute(select(EntityMapping.source_system))).scalars().all()
        )
        assert EXPECTED_SYSTEMS.issubset(systems)

    async def test_dates_and_source_values_contract(self, dbSession) -> None:
        """有效期契约：effective=2026-01-01、expiry=NULL；ERP 供应商源键 V00000X 形态。"""
        await seedEntityMappings(dbSession)
        rows = (
            await dbSession.execute(select(EntityMapping))
        ).scalars().all()
        assert all(r.effective_date == EXPECTED_EFFECTIVE for r in rows)
        assert all(r.expiry_date is None for r in rows)
        # 抽查：第 1 家供应商 ERP 映射的源键形态
        first = next(
            r
            for r in rows
            if r.entity_type == "SUPPLIER"
            and r.enterprise_key == 100001
            and r.source_system == SourceSystem.ERP
        )
        assert first.source_key == "V000001"
        assert first.source_code == "V000001"
        assert first.enterprise_code == "SUP000001"

    async def test_po_gr_iqc_use_business_key_rule(self, dbSession) -> None:
        await seedEntityMappings(dbSession)
        rows = (
            await dbSession.execute(
                select(EntityMapping).where(
                    EntityMapping.entity_type.in_(
                        ["PO", "GR", "IQC"]
                    )
                )
            )
        ).scalars().all()
        assert len(rows) == EXPECTED_BUSINESS_KEY_COUNT
        assert all(m.match_rule == MatchRule.BUSINESS_KEY for m in rows)

    async def test_supplier_material_use_mdm_master_rule(self, dbSession) -> None:
        await seedEntityMappings(dbSession)
        rows = (
            await dbSession.execute(
                select(EntityMapping).where(
                    EntityMapping.entity_type.in_(
                        ["SUPPLIER", "MATERIAL"]
                    )
                )
            )
        ).scalars().all()
        assert len(rows) == EXPECTED_MDM_MASTER_COUNT
        assert all(m.match_rule == MatchRule.MDM_MASTER for m in rows)

    async def test_api_list_returns_seeded_rows(self, client, dbSession) -> None:
        """完整 API 链路：种子写入后，HTTP 列表能查到 45 条。"""
        await seedEntityMappings(dbSession)
        resp = await client.get("/api/v1/entity-mappings")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == EXPECTED_TOTAL
        # 抽查：10 条供应商 × ERP，每条的 enterpriseCode 满足 SUP 前缀
        supplier_erp = [
            r
            for r in rows
            if r["entityType"] == "SUPPLIER" and r["sourceSystem"] == "ERP"
        ]
        assert len(supplier_erp) == 10
        assert all(r["enterpriseCode"].startswith("SUP") for r in supplier_erp)
