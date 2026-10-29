"""0136/0137 迁移后的真库结构断言（真 PostgreSQL，非 sqlite）。

只断言结构，不依赖任何业务数据。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def session(dbSession: AsyncSession) -> AsyncSession:
    return dbSession


async def _column_names(session: AsyncSession, table: str) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t"
        ),
        {"t": table},
    )
    return {r[0] for r in rows.all()}


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("ontology_metric", "metric_type"),
        ("ontology_metric", "numerator_measure"),
        ("ontology_metric", "denominator_measure"),
        ("ontology_metric", "aggregation_order"),
        ("ontology_metric", "grain_keys"),
        ("ontology_metric", "filter_expression"),
        ("ontology_metric", "status"),
        ("ontology_metric", "owner"),
        ("ontology_metric", "execution_source"),
        ("kpi_catalog", "grain_keys"),
        ("kpi_catalog", "numerator_columns"),
        ("kpi_catalog", "denominator_columns"),
    ],
)
async def test_new_columns_exist(
    session: AsyncSession, table: str, column: str
) -> None:
    cols = await _column_names(session, table)
    assert column in cols, f"{table} 缺列 {column}"


async def test_metric_source_table_exists(session: AsyncSession) -> None:
    cols = await _column_names(session, "ontology_metric_source")
    assert cols == {"id", "metric_id", "class_id", "role"}


async def test_metric_source_unique_constraint_exists(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'ontology_metric_source'::regclass "
            "AND contype = 'u'"
        )
    )
    names = {r[0] for r in rows.all()}
    assert names, "ontology_metric_source 缺 UNIQUE 约束"


async def test_metric_source_index_on_class_id(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'ontology_metric_source'"
        )
    )
    names = {r[0] for r in rows.all()}
    assert any("class_id" in n for n in names), f"缺 class_id 索引：{names}"


async def test_new_columns_are_nullable_and_writable(
    session: AsyncSession,
) -> None:
    """新列必须可空 + 可写（不破坏既有行）。"""
    row = await session.execute(
        text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'ontology_metric' "
            "AND column_name IN ('metric_type','status')"
        )
    )
    for name, nullable in row.all():
        assert nullable == "NO", f"{name} 不应可空"
    # 新列可写：插一行再删掉
    await session.execute(
        text(
            "INSERT INTO ontology_metric "
            "(metric_name, formula, metric_type, status, created_time, updated_time) "
            "VALUES ('__schema_probe__', 'x', 'MEASURE', 'DRAFT', NOW(), NOW())"
        )
    )
    await session.execute(
        text("DELETE FROM ontology_metric WHERE metric_name = '__schema_probe__'")
    )
    await session.commit()


async def test_metric_source_unique_blocks_duplicate_triple(
    session: AsyncSession,
) -> None:
    """同一 (metric_id, class_id, role) 不得重复。"""
    from sqlalchemy.exc import IntegrityError

    metric_id = await _seedProbeMetric(session)
    class_id = await _seedProbeClass(session)
    await session.execute(
        text(
            "INSERT INTO ontology_metric_source (metric_id, class_id, role) "
            "VALUES (:m, :c, 'BASE')"
        ),
        {"m": metric_id, "c": class_id},
    )
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO ontology_metric_source (metric_id, class_id, role) "
                "VALUES (:m, :c, 'BASE')"
            ),
            {"m": metric_id, "c": class_id},
        )
    await session.rollback()


async def test_metric_source_allows_same_class_different_role(
    session: AsyncSession,
) -> None:
    """同一 (metric_id, class_id) 不同 role 必须允许（UNIQUE 是三元组）。"""
    metric_id = await _seedProbeMetric(session)
    class_id = await _seedProbeClass(session)
    await session.execute(
        text(
            "INSERT INTO ontology_metric_source (metric_id, class_id, role) "
            "VALUES (:m, :c, 'NUMERATOR')"
        ),
        {"m": metric_id, "c": class_id},
    )
    await session.execute(
        text(
            "INSERT INTO ontology_metric_source (metric_id, class_id, role) "
            "VALUES (:m, :c, 'DENOMINATOR')"
        ),
        {"m": metric_id, "c": class_id},
    )
    await session.commit()
    rows = await session.execute(
        text(
            "SELECT role FROM ontology_metric_source WHERE metric_id = :m AND class_id = :c"
        ),
        {"m": metric_id, "c": class_id},
    )
    assert {r[0] for r in rows.all()} == {"NUMERATOR", "DENOMINATOR"}


async def _seedProbeMetric(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO ontology_metric "
            "(metric_name, formula, created_time, updated_time) "
            "VALUES ('__probe_metric__', 'x', NOW(), NOW()) RETURNING id"
        )
    )
    return int(row.scalar_one())


async def _seedProbeClass(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO ontology_class "
            "(class_name, version, valid_from, created_time, updated_time) "
            "VALUES ('__probe_class__', 1, NOW(), NOW(), NOW()) RETURNING id"
        )
    )
    return int(row.scalar_one())
