"""运行时本体绑定解析器（集成）：真实 PG + 真实绑定表。

测试库（5434 `qa_metadata_test_mainline`）**没有**任何 `procurement_source_binding`
行 ⇒ 本文件自带种数夹具（真实 ORM 行，走真实 CHECK / 唯一索引）。

刻意把 `ontology_class.source_table` 与 `ontology_property.source_column` 写成
**过期值**（`STALE_*`）⇒ 解析器一旦回退到本体表/列（而不是读绑定）就会露馅。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSourceType
from app.domain.models import DataSource, OntologyClass, OntologyProperty
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)
from app.services.ontology_binding_resolver import (
    THBI_DATASOURCE_ID,
    THBI_SCHEMA_NAME,
    BindingResolutionError,
    resolveBinding,
)

_ORDER_DTL_CLASS = "DWD_PURCHASE_ORDER_DTL"
_ORDER_DTL_COLUMNS = {
    "PO_NO": "PO_NO",
    "PO_QTY": "PO_QTY",
    "ORDER_DATE": "ORDER_DATE",
}
_CANDIDATE_CLASS = "DWD_PURCHASE_ORDER_DTL_CANDIDATE"
_TOMBSTONE_CLASS = "DWD_PURCHASE_ORDER_DTL_TOMBSTONED"


async def _seedThbiDataSource(dbSession: AsyncSession) -> DataSource:
    """种 THBI 数据源（id 必须 = 1 ⇒ 硬规则的数据源闸门才放行）。"""
    dataSource = DataSource(
        name="THBI Oracle",
        type=DataSourceType.ORACLE,
        host="oracle-thbi.internal",
        port=1521,
        database_name="THBI",
        username="ZJTH",
        password_encrypted="encrypted-placeholder",
        is_active=True,
        oracle_version="19c",
    )
    dbSession.add(dataSource)
    await dbSession.flush()
    assert dataSource.id == THBI_DATASOURCE_ID, (
        f"THBI 数据源 id 应为 {THBI_DATASOURCE_ID}，实得 {dataSource.id}"
    )
    return dataSource


async def _seedClassWithProperties(
    dbSession: AsyncSession,
    *,
    className: str,
    propertyColumns: dict[str, str],
    isTombstone: bool = False,
) -> tuple[int, dict[str, int]]:
    """种本体类 + 其属性，返回 `(classId, {属性名: propertyId})`。

    刻意把 `ontology_class.source_table` / `ontology_property.source_column`
    写成**过期值**（`STALE_*`）⇒ 解析器若回退到本体表/列就会露馅。
    """
    ontologyClass = OntologyClass(
        class_name=className,
        source_table=f"STALE_{className}",
        version=1,
        created_by="test-binding-resolver",
    )
    if isTombstone:
        ontologyClass.valid_to = datetime.now(UTC)
    dbSession.add(ontologyClass)
    await dbSession.flush()

    propertyByName: dict[str, OntologyProperty] = {}
    for propertyName in propertyColumns:
        prop = OntologyProperty(
            class_id=ontologyClass.id,
            property_name=propertyName,
            data_type="string",
            is_primary_key=False,
            is_foreign_key=False,
            source_column=f"STALE_{propertyName}",
        )
        dbSession.add(prop)
        propertyByName[propertyName] = prop
    await dbSession.flush()
    # id 是自增主键 ⇒ flush 之后才有值（flush 前读 id 会拿到 None）。
    propertyIdByName = {name: prop.id for name, prop in propertyByName.items()}
    return ontologyClass.id, propertyIdByName


async def _seedBindings(
    dbSession: AsyncSession,
    *,
    classId: int,
    propertyIdByName: dict[str, int],
    propertyColumns: dict[str, str],
    sourceTable: str,
    isCandidate: bool = False,
) -> None:
    """种源绑定 + 属性绑定（候选行不带确认人 ⇒ 过 DB CHECK 约束）。"""
    sourceBinding = ProcurementSourceBinding(
        class_id=classId,
        datasource_id=THBI_DATASOURCE_ID,
        schema_name=THBI_SCHEMA_NAME,
        source_table=sourceTable,
        binding_role="BASE",
        is_candidate=isCandidate,
        confirmed_by=None if isCandidate else "test-binding-resolver",
        confirmed_time=None if isCandidate else datetime.now(UTC),
    )
    dbSession.add(sourceBinding)
    await dbSession.flush()

    for propertyName, sourceColumn in propertyColumns.items():
        dbSession.add(
            ProcurementPropertyBinding(
                source_binding_id=sourceBinding.id,
                property_id=propertyIdByName[propertyName],
                source_column=sourceColumn,
                is_candidate=isCandidate,
                confirmed_by=None if isCandidate else "test-binding-resolver",
                confirmed_time=None if isCandidate else datetime.now(UTC),
            )
        )
    await dbSession.commit()


async def _seedClassWithBinding(
    dbSession: AsyncSession,
    *,
    className: str,
    propertyColumns: dict[str, str],
    sourceTable: str,
    isCandidate: bool = False,
    isTombstone: bool = False,
) -> int:
    """种「本体类 + 属性 + 源绑定 + 属性绑定」四件套，返回 classId。"""
    classId, propertyIdByName = await _seedClassWithProperties(
        dbSession,
        className=className,
        propertyColumns=propertyColumns,
        isTombstone=isTombstone,
    )
    await _seedBindings(
        dbSession,
        classId=classId,
        propertyIdByName=propertyIdByName,
        propertyColumns=propertyColumns,
        sourceTable=sourceTable,
        isCandidate=isCandidate,
    )
    return classId


@pytest_asyncio.fixture
async def seedThbiOrderDtlBinding(dbSession: AsyncSession) -> None:
    """种一条确认生效的 THBI 订单行绑定（含 PO_NO / PO_QTY / ORDER_DATE）。"""
    await _seedThbiDataSource(dbSession)
    await _seedClassWithBinding(
        dbSession,
        className=_ORDER_DTL_CLASS,
        propertyColumns=_ORDER_DTL_COLUMNS,
        sourceTable=_ORDER_DTL_CLASS,
    )


@pytest.mark.asyncio
async def test_resolve_order_dtl_binding_from_real_db(
    dbSession, seedThbiOrderDtlBinding
) -> None:
    """本体已绑的订单行类能解析出 THBI 物理表与属性列。"""
    resolved = await resolveBinding(
        dbSession,
        className="DWD_PURCHASE_ORDER_DTL",
        requiredProperties=["PO_NO", "PO_QTY", "ORDER_DATE"],
    )
    assert resolved.qualifiedTable() == "THBI.DWD_PURCHASE_ORDER_DTL"
    assert resolved.datasourceId == THBI_DATASOURCE_ID
    assert resolved.column("PO_QTY") == "PO_QTY"


@pytest.mark.asyncio
async def test_resolve_missing_property_fails_closed(
    dbSession, seedThbiOrderDtlBinding
) -> None:
    """必需属性未绑定 ⇒ fail-closed，不回退硬编码列。"""
    with pytest.raises(BindingResolutionError):
        await resolveBinding(
            dbSession,
            className="DWD_PURCHASE_ORDER_DTL",
            requiredProperties=["NOT_A_REAL_PROPERTY"],
        )


@pytest.mark.asyncio
async def test_resolve_unknown_class_fails_closed(
    dbSession, seedThbiOrderDtlBinding
) -> None:
    with pytest.raises(BindingResolutionError):
        await resolveBinding(
            dbSession,
            className="NO_SUCH_CLASS_XYZ",
            requiredProperties=["PO_NO"],
        )


@pytest.mark.asyncio
async def test_physical_names_come_from_binding_not_ontology_table(
    dbSession, seedThbiOrderDtlBinding
) -> None:
    """物理表名 / 列名只从绑定读 —— 不回退本体表里的过期值。"""
    resolved = await resolveBinding(
        dbSession,
        className="DWD_PURCHASE_ORDER_DTL",
        requiredProperties=["PO_QTY"],
    )
    assert resolved.sourceTable == "DWD_PURCHASE_ORDER_DTL"
    assert resolved.sourceTable != "STALE_DWD_PURCHASE_ORDER_DTL"
    assert resolved.column("PO_QTY") == "PO_QTY"
    assert resolved.column("PO_QTY") != "STALE_PO_QTY"


@pytest.mark.asyncio
async def test_resolve_candidate_only_binding_fails_closed(dbSession) -> None:
    """只有候选绑定 ⇒ fail-closed（候选不是结论，不得进读路径）。"""
    await _seedThbiDataSource(dbSession)
    await _seedClassWithBinding(
        dbSession,
        className=_CANDIDATE_CLASS,
        propertyColumns={"PO_NO": "PO_NO"},
        sourceTable=_CANDIDATE_CLASS,
        isCandidate=True,
    )
    with pytest.raises(BindingResolutionError):
        await resolveBinding(
            dbSession,
            className=_CANDIDATE_CLASS,
            requiredProperties=["PO_NO"],
        )


@pytest.mark.asyncio
async def test_resolve_tombstoned_class_fails_closed(dbSession) -> None:
    """墓碑类（valid_to 非空）⇒ fail-closed。"""
    await _seedThbiDataSource(dbSession)
    await _seedClassWithBinding(
        dbSession,
        className=_TOMBSTONE_CLASS,
        propertyColumns={"PO_NO": "PO_NO"},
        sourceTable=_TOMBSTONE_CLASS,
        isTombstone=True,
    )
    with pytest.raises(BindingResolutionError):
        await resolveBinding(
            dbSession,
            className=_TOMBSTONE_CLASS,
            requiredProperties=["PO_NO"],
        )


@pytest.mark.asyncio
async def test_resolve_rejects_non_thbi_datasource(
    dbSession, seedThbiOrderDtlBinding
) -> None:
    """硬规则：调用方不得指定别的数据源。"""
    with pytest.raises(BindingResolutionError):
        await resolveBinding(
            dbSession,
            className="DWD_PURCHASE_ORDER_DTL",
            requiredProperties=["PO_NO"],
            datasourceId=2,
        )
