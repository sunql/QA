"""LocalImportService 关联推断 + 列选 集成测试。

用 fake schema（含 Sage X3 命名约定的 THBI 风格表）驱动 build_preview，
验证三件事：
- 声明外键（declared_fk）与列名约定（name_convention）推断的开关与标注；
- 两者产同一条边时去重只保留一条；
- selected_columns 部分列导入：属性只生成选中列，且引用未导入列的 join 一并丢弃
  （避免 join 指向未落库的 property）。

依赖真实 PG（dbSession fixture，见 services/conftest）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSourceType
from app.domain.models import DataSource
from app.domain.schemas import (
    ColumnSchemaRead,
    ForeignKeySchemaRead,
    ImportPreviewRequest,
    ImportRuleConfig,
    SchemaIntrospectResponse,
    TableSchemaRead,
)
from app.services.local_import_service import LocalImportService


class _FakeOntologyService:
    """预览路径只需空的 class/property 列表，冲突检测视为全新建。"""

    async def listClasses(self, session, includeExpired=False):
        return []

    async def listPropertiesByClass(self, session, classId):
        return []


def _col(name: str, data_type: str = "VARCHAR2") -> ColumnSchemaRead:
    return ColumnSchemaRead(column_name=name, data_type=data_type, nullable=True)


def _sage_schema() -> SchemaIntrospectResponse:
    """THBI 风格：零声明外键，仅靠列名约定表达引用。"""
    return SchemaIntrospectResponse(
        tables=[
            TableSchemaRead(
                table_name="ITMMASTER",
                columns=[_col("ITMREF_0"), _col("ITMDES1_0")],
                primary_keys=["ITMREF_0"],
                foreign_keys=[],
            ),
            TableSchemaRead(
                table_name="BPCUSTOMER",
                columns=[_col("BPCNUM_0"), _col("BPCNAM_0")],
                primary_keys=["BPCNUM_0"],
                foreign_keys=[],
            ),
            TableSchemaRead(
                table_name="PORDER",
                columns=[_col("POHNUM_0"), _col("BPCNUM_0"), _col("ORDDAT_0")],
                primary_keys=["POHNUM_0"],
                foreign_keys=[],
            ),
            TableSchemaRead(
                table_name="PORDERQ",
                columns=[
                    _col("POHNUM_0"),
                    _col("POPLIN_0"),
                    _col("ITMREF_0"),
                    _col("QTYUOM_0"),
                ],
                primary_keys=[],
                foreign_keys=[],
            ),
        ],
        cached_at=datetime(2026, 8, 29, 0, 0, 0),
    )


async def _seed_datasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        name="thbi-fake",
        type=DataSourceType.ORACLE,
        host="localhost",
        port=1521,
        database_name="THBIDB",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


def _make_service(schema: SchemaIntrospectResponse) -> LocalImportService:
    async def _introspect(self, session, ds, owner: str | None = None) -> None:
        return None

    schema_svc = type(
        "FakeSchemaService",
        (),
        {
            "introspectAndCache": _introspect,
            "buildResponse": lambda self, cache: schema,
        },
    )()
    return LocalImportService(
        schema_service=schema_svc,
        ontology_service=_FakeOntologyService(),
    )


async def _preview(
    dbSession: AsyncSession,
    svc: LocalImportService,
    ds: DataSource,
    *,
    rules: ImportRuleConfig | None = None,
    selected_columns: dict[str, list[str]] | None = None,
):
    request = ImportPreviewRequest(
        rules=rules or ImportRuleConfig(),
        selected_columns=selected_columns,
    )
    return await svc.build_preview(
        dbSession,
        datasource_id=ds.id,
        rules=request.rules,
        selected_columns=request.selected_columns,
    )


@pytest.mark.asyncio
async def test_build_preview_infers_name_convention_joins(dbSession: AsyncSession):
    """零声明 FK 的 THBI 风格表集 → 按列名约定推断 join，并标注 inferred_by。"""
    ds = await _seed_datasource(dbSession)
    svc = _make_service(_sage_schema())

    result = await _preview(dbSession, svc, ds)

    keyed = {
        (j.source_table, tuple(j.source_columns), j.target_table): j
        for j in result.proposed_joins
    }
    assert ("PORDERQ", ("POHNUM_0",), "PORDER") in keyed  # 明细 → 订单头
    assert ("PORDERQ", ("ITMREF_0",), "ITMMASTER") in keyed  # 明细 → 物料
    assert ("PORDER", ("BPCNUM_0",), "BPCUSTOMER") in keyed  # 订单 → 客户
    for j in result.proposed_joins:
        assert j.inferred_by == "name_convention"
        assert j.relation_type == "foreign_key"
        assert j.is_selected is True


@pytest.mark.asyncio
async def test_build_preview_name_convention_toggle_off(dbSession: AsyncSession):
    """关闭列名约定推断 → 零声明 FK 表集不再产出 join。"""
    ds = await _seed_datasource(dbSession)
    svc = _make_service(_sage_schema())
    rules = ImportRuleConfig()
    rules.join_inference.infer_name_convention = False

    result = await _preview(dbSession, svc, ds, rules=rules)

    assert result.proposed_joins == []


@pytest.mark.asyncio
async def test_declared_fk_joins_labeled_and_deduped_with_convention(
    dbSession: AsyncSession,
):
    """声明 FK 命中且列名约定同时命中同一条边 → 只保留一条，标注 declared_fk。"""
    ds = await _seed_datasource(dbSession)
    schema = _sage_schema()
    # 给 PORDERQ.ITMREF_0 补一条显式声明 FK（与约定指向同一目标）
    for table in schema.tables:
        if table.table_name == "PORDERQ":
            table.foreign_keys = [
                ForeignKeySchemaRead(
                    column_name="ITMREF_0",
                    ref_table="ITMMASTER",
                    ref_column="ITMREF_0",
                )
            ]
    svc = _make_service(schema)

    result = await _preview(dbSession, svc, ds)

    itm_edges = [
        j
        for j in result.proposed_joins
        if j.source_table == "PORDERQ" and j.target_table == "ITMMASTER"
    ]
    assert len(itm_edges) == 1  # 声明 + 约定去重
    assert itm_edges[0].inferred_by == "declared_fk"  # 声明 FK 先入，保留其标注


@pytest.mark.asyncio
async def test_selected_columns_limits_properties_to_whitelist(dbSession: AsyncSession):
    """单表部分列导入：仅选中列生成属性，未选列（含引用列）不生成。"""
    ds = await _seed_datasource(dbSession)
    svc = _make_service(_sage_schema())

    result = await _preview(
        dbSession,
        svc,
        ds,
        selected_columns={"PORDERQ": ["POHNUM_0", "POPLIN_0", "QTYUOM_0"]},
    )

    porderq = next(c for c in result.proposed_classes if c.source_table == "PORDERQ")
    assert {p.source_column for p in porderq.properties} == {
        "POHNUM_0",
        "POPLIN_0",
        "QTYUOM_0",
    }
    assert "ITMREF_0" not in {p.source_column for p in porderq.properties}


@pytest.mark.asyncio
async def test_selected_columns_prunes_joins_on_unimported_reference_column(
    dbSession: AsyncSession,
):
    """列选把某 join 的引用列剔除 → 该 join 不再预览（避免指向未落库的 property）。

    PORDERQ 未选 ITMREF_0：到 ITMMASTER 的 join 丢弃；POHNUM_0 仍在 → 到 PORDER
    的 join 保留。目标表未做列选，不受影响。
    """
    ds = await _seed_datasource(dbSession)
    svc = _make_service(_sage_schema())

    result = await _preview(
        dbSession,
        svc,
        ds,
        selected_columns={"PORDERQ": ["POHNUM_0", "POPLIN_0", "QTYUOM_0"]},
    )

    pairs = {
        (j.source_table, tuple(j.source_columns), j.target_table)
        for j in result.proposed_joins
    }
    assert ("PORDERQ", ("ITMREF_0",), "ITMMASTER") not in pairs  # 引用列未导入 → 丢弃
    assert ("PORDERQ", ("POHNUM_0",), "PORDER") in pairs  # 引用列已导入 → 保留


@pytest.mark.asyncio
async def test_target_column_whitelist_prunes_dangling_target(dbSession: AsyncSession):
    """目标表做列选且剔除 join 的目标键列 → 该 join 丢弃，避免悬空目标属性。"""
    ds = await _seed_datasource(dbSession)
    svc = _make_service(_sage_schema())

    result = await _preview(
        dbSession,
        svc,
        ds,
        selected_columns={"ITMMASTER": ["ITMDES1_0"]},  # 目标键 ITMREF_0 未导入
    )

    itm_edges = [
        j
        for j in result.proposed_joins
        if j.target_table == "ITMMASTER"
    ]
    assert itm_edges == []


@pytest.mark.asyncio
async def test_full_import_keeps_all_columns_and_joins(dbSession: AsyncSession):
    """无 selected_columns → 全列属性 + 全部约定 join，与旧行为一致（不回归）。"""
    ds = await _seed_datasource(dbSession)
    svc = _make_service(_sage_schema())

    result = await _preview(dbSession, svc, ds)

    assert len(result.proposed_classes) == 4
    total_props = sum(len(c.properties) for c in result.proposed_classes)
    assert total_props == sum(len(t.columns) for t in _sage_schema().tables)
    assert len(result.proposed_joins) == 3
