"""Task 1.5: seed_kpi_semantic_index 集成测试（TDD RED -> GREEN）。

覆盖：
1. seed 后所有 PUBLISHED KPI 的 semantic_keywords 非空且长度 >= 3
2. 重复运行不会报错且会更新（on_conflict_do_update 幂等性）
3. DRAFT 状态的 KPI 不被 seed 触碰

依赖真实 PostgreSQL（qa_metadata_test），禁止 sqlite 内存库。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog


class TestSeedKpiSemanticIndex:
    """seed_kpi_semantic_index 行为验证。"""

    @pytest.mark.asyncio
    async def test_seed_writes_semantic_keywords_for_all_published_kpis(
        self, dbSession: AsyncSession
    ) -> None:
        """seed 后所有 PUBLISHED KPI 都有 semantic_keywords 非空且长度 >= 3。"""
        # 1. 先 seed KPI catalog（12 条 PUBLISHED）
        from scripts.seed_kpi_catalog import seedKpiCatalog

        await seedKpiCatalog(dbSession)

        # 2. 跑 semantic index seed
        from scripts.seed_kpi_semantic_index import seedKpiSemanticIndex

        await seedKpiSemanticIndex(dbSession)

        # 3. 查所有 PUBLISHED KPI
        result = await dbSession.execute(
            select(KpiCatalog.kpi_code, KpiCatalog.semantic_keywords).where(
                KpiCatalog.status == "PUBLISHED"
            )
        )
        rows = result.all()

        # 4. 断言每个都有 semantic_keywords 非空且长度 >= 3
        assert len(rows) >= 1, "库内至少应有 1 条 PUBLISHED KPI"
        for kpi_code, keywords in rows:
            assert keywords is not None, f"{kpi_code} 的 semantic_keywords 不应为 NULL"
            assert len(keywords) >= 3, (
                f"{kpi_code} 的 semantic_keywords 长度应 >= 3，实际 {len(keywords)}"
            )

    @pytest.mark.asyncio
    async def test_seed_uses_on_conflict_do_update_idempotent(
        self, dbSession: AsyncSession
    ) -> None:
        """seed 第二次跑不会报错且会更新（幂等性）。"""
        from scripts.seed_kpi_catalog import seedKpiCatalog
        from scripts.seed_kpi_semantic_index import seedKpiSemanticIndex

        await seedKpiCatalog(dbSession)

        # 第一次 seed
        await seedKpiSemanticIndex(dbSession)

        # 用 raw SQL 直接查（绕开 SQLAlchemy identity map）
        row_before = (
            await dbSession.execute(
                text(
                    "SELECT semantic_keywords FROM kpi_catalog "
                    "WHERE kpi_code = 'KPI_SUPPLIER_OTD'"
                )
            )
        ).fetchone()

        assert row_before is not None
        keywords_before = row_before[0]
        assert keywords_before is not None

        # 第二次 seed（不应报错）
        await seedKpiSemanticIndex(dbSession)

        # 验证 keywords 与第一次一致（DO UPDATE 覆盖为 seed 定义的值）
        row_after = (
            await dbSession.execute(
                text(
                    "SELECT semantic_keywords FROM kpi_catalog "
                    "WHERE kpi_code = 'KPI_SUPPLIER_OTD'"
                )
            )
        ).fetchone()
        keywords_after = row_after[0]

        assert keywords_after == keywords_before, (
            "第二次 seed 后 keywords 应与第一次一致（幂等）"
        )

    @pytest.mark.asyncio
    async def test_seed_skips_draft_kpis(self, dbSession: AsyncSession) -> None:
        """status=DRAFT 的 KPI 不被 seed 触碰（semantic_keywords 保持 NULL）。"""
        # 插入一条 DRAFT KPI（绕开 ORM 以确保 raw SQL 行为）
        await dbSession.execute(
            text(
                "INSERT INTO kpi_catalog (kpi_code, kpi_name, status, version, revision_count, created_time, updated_time) "
                "VALUES ('KPI_DRAFT_TEST', '测试草稿', 'DRAFT', 'v1.0', 0, NOW(), NOW()) "
                "ON CONFLICT (kpi_code) DO NOTHING"
            )
        )
        await dbSession.commit()

        # 跑 seed
        from scripts.seed_kpi_semantic_index import seedKpiSemanticIndex

        await seedKpiSemanticIndex(dbSession)

        # DRAFT KPI 的 semantic_keywords 应仍是 NULL
        row = (
            await dbSession.execute(
                text(
                    "SELECT semantic_keywords FROM kpi_catalog "
                    "WHERE kpi_code = 'KPI_DRAFT_TEST'"
                )
            )
        ).fetchone()

        assert row is not None
        assert row[0] is None, "DRAFT KPI 的 semantic_keywords 应保持 NULL"

    @pytest.mark.asyncio
    async def test_seed_sets_match_threshold_to_075(
        self, dbSession: AsyncSession
    ) -> None:
        """seed 后 PUBLISHED KPI 的 match_threshold 应为 0.75。"""
        from scripts.seed_kpi_catalog import seedKpiCatalog
        from scripts.seed_kpi_semantic_index import seedKpiSemanticIndex

        await seedKpiCatalog(dbSession)
        await seedKpiSemanticIndex(dbSession)

        row = (
            await dbSession.execute(
                text(
                    "SELECT match_threshold FROM kpi_catalog "
                    "WHERE kpi_code = 'KPI_SUPPLIER_OTD'"
                )
            )
        ).fetchone()

        assert row is not None
        threshold = Decimal(str(row[0]))
        assert threshold == Decimal("0.75"), (
            f"match_threshold 应为 0.75，实际 {threshold}"
        )
