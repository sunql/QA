"""Task 1.1 RED test — kpi_catalog has semantic_keywords + match_threshold columns.

RED 阶段：验证 schema 有新列（不依赖 model 改动，独立检查 DB schema）。
GREEN 阶段：migration + model 改完后测试通过。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL（qa_metadata_test）。
"""

from __future__ import annotations

from sqlalchemy import text

from app.domain.models import KpiCatalog


class TestKpiCatalogSemanticFields:
    """验证 kpi_catalog 表新增字段的 schema 存在性。"""

    async def test_kpi_catalog_has_semantic_keywords_field(self, dbSession) -> None:
        """semantic_keywords 列存在且类型为 text[]（PostgreSQL array of varchar）。"""
        # information_schema.columns 只检查列是否存在 + data_type = 'ARRAY'
        result = await dbSession.execute(
            text(
                "SELECT data_type "
                "FROM information_schema.columns "
                "WHERE table_name = 'kpi_catalog' AND column_name = 'semantic_keywords'"
            )
        )
        row = result.fetchone()
        assert row is not None, (
            "kpi_catalog.semantic_keywords 列不存在，请检查 migration 是否已 apply"
        )
        assert row[0] == "ARRAY", (
            f"semantic_keywords data_type 应为 ARRAY，实际 {row[0]}"
        )
        # pg_attribute 检查元素类型（String(64) → varchar(64)）
        elem_result = await dbSession.execute(
            text(
                "SELECT format_type(typelem, NULL) "
                "FROM pg_attribute "
                "JOIN pg_type ON pg_type.oid = pg_attribute.atttypid "
                "WHERE attrelid = 'kpi_catalog'::regclass "
                "  AND attname = 'semantic_keywords' "
                "  AND attndims > 0"
            )
        )
        elem_row = elem_result.fetchone()
        assert elem_row is not None, "semantic_keywords 应为多维数组（attndims > 0）"
        # String(64) 在 PG 底层映射为 character varying，数组元素类型为 character varying
        assert "character varying" in elem_row[0], (
            f"semantic_keywords 元素类型应为 character varying，实际 {elem_row[0]}"
        )

    async def test_kpi_catalog_has_match_threshold_field(self, dbSession) -> None:
        """match_threshold 列存在且类型为 numeric(3,2)，非空，默认 0.75。"""
        result = await dbSession.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale, "
                "       is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = 'kpi_catalog' AND column_name = 'match_threshold'"
            )
        )
        row = result.fetchone()
        assert row is not None, (
            "kpi_catalog.match_threshold 列不存在，请检查 migration 是否已 apply"
        )
        data_type, precision, scale, nullable, default = row
        assert data_type == "numeric", f"match_threshold data_type 应为 numeric，实际 {data_type}"
        assert precision == 3, f"match_threshold precision 应为 3，实际 {precision}"
        assert scale == 2, f"match_threshold scale 应为 2，实际 {scale}"
        assert nullable == "NO", f"match_threshold 应为 NOT NULL，实际 {nullable}"
        assert default is not None and "0.75" in str(default), (
            f"match_threshold 应有 server_default '0.75'，实际 {default}"
        )

    async def test_kpi_catalog_semantic_keywords_gin_index_exists(self, dbSession) -> None:
        """semantic_keywords GIN 索引存在。"""
        result = await dbSession.execute(
            text(
                "SELECT indexname, indexdef "
                "FROM pg_indexes "
                "WHERE tablename = 'kpi_catalog' AND indexname = 'ix_kpi_catalog_semantic_keywords'"
            )
        )
        row = result.fetchone()
        assert row is not None, (
            "ix_kpi_catalog_semantic_keywords 索引不存在，请检查 migration 是否已 apply"
        )
        indexname, indexdef = row
        assert "gin" in indexdef.lower(), (
            f"ix_kpi_catalog_semantic_keywords 应使用 GIN 索引，实际 {indexdef}"
        )

    async def test_kpi_catalog_model_reflects_new_fields(self, dbSession) -> None:
        """Model 层已知字段：semantic_keywords（list[str]|None）+ match_threshold（Decimal）。"""
        # KpiCatalog model 上检查属性名存在（model 改动后）
        assert hasattr(KpiCatalog, "semantic_keywords"), (
            "KpiCatalog 缺少 semantic_keywords 属性，请更新 models.py"
        )
        assert hasattr(KpiCatalog, "match_threshold"), (
            "KpiCatalog 缺少 match_threshold 属性，请更新 models.py"
        )
