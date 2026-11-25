"""运行时本体绑定解析器（集成）：真实 PG + 真实绑定表。

测试库（5434 `qa_metadata_test_mainline`）**没有**任何 `procurement_source_binding`
行 ⇒ 本文件自带种数夹具（真实 ORM 行，走真实 CHECK / 部分唯一索引）。

刻意把 `ontology_class.source_table` 与 `ontology_property.source_column` 写成
**过期值**（`STALE_*`）⇒ 解析器一旦回退到本体表/列（而不是读绑定）就会露馅。

末尾另有 Task 2 种子**交付函数**的一条真实 PG 行为用例（建行 → 确认 → 幂等 → 可解析）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSourceType
from app.domain.models import DataSource, OntologyClass, OntologyProperty
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)
from app.infrastructure import database as dbModule
from app.services.ontology_binding_resolver import (
    THBI_DATASOURCE_ID,
    THBI_SCHEMA_NAME,
    BindingResolutionError,
    resolveBinding,
)
from scripts.seed_procurement_bindings import (
    FACILITY_BINDING,
    seed_facility_binding,
)

_ORDER_DTL_CLASS = "DWD_PURCHASE_ORDER_DTL"
_ORDER_DTL_COLUMNS = {
    "PO_NO": "PO_NO",
    "PO_QTY": "PO_QTY",
    "ORDER_DATE": "ORDER_DATE",
}
_FACILITY_CLASS = "DIM_FACILITY"
#: `DIM_FACILITY` 在 P5 到货场景的必需属性（基地主键 + 基地名）。
_FACILITY_COLUMNS = {
    "FCY_0": "FCY_0",
    "FCYNAM_0": "FCYNAM_0",
}
_CANDIDATE_CLASS = "DWD_PURCHASE_ORDER_DTL_CANDIDATE"
_TOMBSTONE_CLASS = "DWD_PURCHASE_ORDER_DTL_TOMBSTONED"
_FUTURE_CLASS = "DWD_PURCHASE_ORDER_DTL_FUTURE"
_FUTURE_PROPERTY_CLASS = "DWD_PURCHASE_ORDER_DTL_FUTURE_PROPERTY"
_MULTI_ROLE_CLASS = "DWD_PURCHASE_ORDER_DTL_MULTI_ROLE"

_ONE_DAY = timedelta(days=1)


async def _seedThbiDataSource(dbSession: AsyncSession) -> DataSource:
    """种 THBI 数据源（id 必须 = 1 ⇒ 硬规则的数据源闸门才放行）。

    账号用 `THBI`（硬规则里的 THBI Oracle 账号）—— 别写成别的库账号，
    后续任务照抄这个夹具会把错身份传播下去。
    """
    dataSource = DataSource(
        name="THBI Oracle",
        type=DataSourceType.ORACLE,
        host="oracle-thbi.internal",
        port=1521,
        database_name="THBI",
        username="THBI",
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
    stalePrefix: str = "STALE_",
) -> tuple[int, dict[str, int]]:
    """种本体类 + 其属性，返回 `(classId, {属性名: propertyId})`。

    默认刻意把 `ontology_class.source_table` / `ontology_property.source_column`
    写成**过期值**（`STALE_*`）⇒ 解析器若回退到本体表/列就会露馅。
    `stalePrefix=""` 用于**本体模型就是物理名真相**的场景（种子脚本从它复制
    表名/列名进绑定；`_assertSourceTable` 也会校验表名一致）。
    """
    ontologyClass = OntologyClass(
        class_name=className,
        source_table=f"{stalePrefix}{className}",
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
            source_column=f"{stalePrefix}{propertyName}",
        )
        dbSession.add(prop)
        propertyByName[propertyName] = prop
    await dbSession.flush()
    # id 是自增主键 ⇒ flush 之后才有值（flush 前读 id 会拿到 None）。
    propertyIdByName = {name: prop.id for name, prop in propertyByName.items()}
    return ontologyClass.id, propertyIdByName


async def _seedSourceBinding(
    dbSession: AsyncSession,
    *,
    classId: int,
    propertyIdByName: dict[str, int],
    propertyColumns: dict[str, str],
    sourceTable: str,
    bindingRole: str = "BASE",
    isCandidate: bool = False,
    validFrom: datetime | None = None,
    propertyValidFrom: datetime | None = None,
) -> int:
    """种一条源绑定 + 其属性绑定，返回 sourceBindingId。

    候选行不带确认人 ⇒ 过 DB CHECK「候选 ↔ 无确认人」互斥约束。
    `validFrom` / `propertyValidFrom` 分别控制源绑定与属性绑定的生效时刻。
    """
    sourceBinding = ProcurementSourceBinding(
        class_id=classId,
        datasource_id=THBI_DATASOURCE_ID,
        schema_name=THBI_SCHEMA_NAME,
        source_table=sourceTable,
        binding_role=bindingRole,
        is_candidate=isCandidate,
        confirmed_by=None if isCandidate else "test-binding-resolver",
        confirmed_time=None if isCandidate else datetime.now(UTC),
        **({} if validFrom is None else {"valid_from": validFrom}),
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
                **(
                    {}
                    if propertyValidFrom is None
                    else {"valid_from": propertyValidFrom}
                ),
            )
        )
    await dbSession.commit()
    return sourceBinding.id


async def _seedClassWithBinding(
    dbSession: AsyncSession,
    *,
    className: str,
    propertyColumns: dict[str, str],
    sourceTable: str,
    bindingRole: str = "BASE",
    isCandidate: bool = False,
    isTombstone: bool = False,
    validFrom: datetime | None = None,
    propertyValidFrom: datetime | None = None,
) -> int:
    """种「本体类 + 属性 + 源绑定 + 属性绑定」四件套，返回 classId。"""
    classId, propertyIdByName = await _seedClassWithProperties(
        dbSession,
        className=className,
        propertyColumns=propertyColumns,
        isTombstone=isTombstone,
    )
    await _seedSourceBinding(
        dbSession,
        classId=classId,
        propertyIdByName=propertyIdByName,
        propertyColumns=propertyColumns,
        sourceTable=sourceTable,
        bindingRole=bindingRole,
        isCandidate=isCandidate,
        validFrom=validFrom,
        propertyValidFrom=propertyValidFrom,
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


@pytest_asyncio.fixture
async def seedThbiFacilityBinding(dbSession: AsyncSession) -> None:
    """种一条确认生效的 THBI 基地字典绑定（含 FCY_0 / FCYNAM_0）。

    与 `seedThbiOrderDtlBinding` 同形：本体表/列仍写 `STALE_*` 过期值，
    物理表名/列名只由绑定表给出 ⇒ 解析器回退本体表就会露馅。
    """
    await _seedThbiDataSource(dbSession)
    await _seedClassWithBinding(
        dbSession,
        className=_FACILITY_CLASS,
        propertyColumns=_FACILITY_COLUMNS,
        sourceTable=_FACILITY_CLASS,
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
    with pytest.raises(BindingResolutionError) as exc:
        await resolveBinding(
            dbSession,
            className="DWD_PURCHASE_ORDER_DTL",
            requiredProperties=["NOT_A_REAL_PROPERTY"],
        )
    assert "缺少必需属性的绑定" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_unknown_class_fails_closed(
    dbSession, seedThbiOrderDtlBinding
) -> None:
    with pytest.raises(BindingResolutionError) as exc:
        await resolveBinding(
            dbSession,
            className="NO_SUCH_CLASS_XYZ",
            requiredProperties=["PO_NO"],
        )
    assert "本体类不存在" in str(exc.value)


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
    with pytest.raises(BindingResolutionError) as exc:
        await resolveBinding(
            dbSession,
            className=_CANDIDATE_CLASS,
            requiredProperties=["PO_NO"],
        )
    assert "没有已确认生效的绑定" in str(exc.value)


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
    with pytest.raises(BindingResolutionError) as exc:
        await resolveBinding(
            dbSession,
            className=_TOMBSTONE_CLASS,
            requiredProperties=["PO_NO"],
        )
    assert "墓碑" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_future_binding_fails_closed(dbSession) -> None:
    """未来才生效的绑定不是「已生效」⇒ 不得进读路径。"""
    await _seedThbiDataSource(dbSession)
    await _seedClassWithBinding(
        dbSession,
        className=_FUTURE_CLASS,
        propertyColumns={"PO_NO": "PO_NO"},
        sourceTable=_FUTURE_CLASS,
        validFrom=datetime.now(UTC) + _ONE_DAY,
    )
    with pytest.raises(BindingResolutionError) as exc:
        await resolveBinding(
            dbSession,
            className=_FUTURE_CLASS,
            requiredProperties=["PO_NO"],
        )
    # 必须是「源绑定未生效」⇒ 不能被「属性缺失」这条更粗的判据掩盖
    assert "没有已确认生效的绑定" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_future_property_binding_fails_closed(dbSession) -> None:
    """源绑定已生效、但属性绑定未来才生效 ⇒ 该属性不可用 ⇒ 必需属性缺失 fail-closed。"""
    await _seedThbiDataSource(dbSession)
    await _seedClassWithBinding(
        dbSession,
        className=_FUTURE_PROPERTY_CLASS,
        propertyColumns={"PO_NO": "PO_NO"},
        sourceTable=_FUTURE_PROPERTY_CLASS,
        propertyValidFrom=datetime.now(UTC) + _ONE_DAY,
    )
    with pytest.raises(BindingResolutionError) as exc:
        await resolveBinding(
            dbSession,
            className=_FUTURE_PROPERTY_CLASS,
            requiredProperties=["PO_NO"],
        )
    assert "缺少必需属性的绑定" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_multi_role_binding_uses_base_only(dbSession) -> None:
    """多角色合法：BASE + FILTER 并存时只认 BASE，FILTER 的列不混进列映射。"""
    await _seedThbiDataSource(dbSession)
    classId, propertyIdByName = await _seedClassWithProperties(
        dbSession,
        className=_MULTI_ROLE_CLASS,
        propertyColumns={"PO_NO": "PO_NO", "PO_QTY": "PO_QTY"},
    )
    await _seedSourceBinding(
        dbSession,
        classId=classId,
        propertyIdByName=propertyIdByName,
        propertyColumns={"PO_NO": "PO_NO"},
        sourceTable=f"{_MULTI_ROLE_CLASS}_BASE",
        bindingRole="BASE",
    )
    await _seedSourceBinding(
        dbSession,
        classId=classId,
        propertyIdByName=propertyIdByName,
        propertyColumns={"PO_QTY": "PO_QTY"},
        sourceTable=f"{_MULTI_ROLE_CLASS}_FILTER",
        bindingRole="FILTER",
    )
    resolved = await resolveBinding(
        dbSession,
        className=_MULTI_ROLE_CLASS,
        requiredProperties=["PO_NO"],
    )
    assert resolved.sourceTable == f"{_MULTI_ROLE_CLASS}_BASE"
    assert resolved.columns == {"PO_NO": "PO_NO"}
    assert "PO_QTY" not in resolved.columns


@pytest.mark.asyncio
async def test_resolve_rejects_non_thbi_datasource(
    dbSession, seedThbiOrderDtlBinding
) -> None:
    """硬规则：调用方不得指定别的数据源。

    必须断言**具体病因**（不是只断言异常类型）：若把 `_assertThbiDatasource`
    这道硬闸删掉，解析会继续走到「数据源 2 没有已确认生效的绑定」并照样抛
    `BindingResolutionError` ⇒ 只断言类型就是假绿。
    `"数据源硬闸"` 是该闸门消息里唯一且稳定的片段（普通失败消息只含「数据源」）。
    """
    with pytest.raises(BindingResolutionError) as exc:
        await resolveBinding(
            dbSession,
            className="DWD_PURCHASE_ORDER_DTL",
            requiredProperties=["PO_NO"],
            datasourceId=2,
        )
    assert "数据源硬闸" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_facility_binding_from_real_db(
    dbSession, seedThbiFacilityBinding
) -> None:
    """DIM_FACILITY 补绑定后可解析（Task 2 种子）。"""
    resolved = await resolveBinding(
        dbSession,
        className="DIM_FACILITY",
        requiredProperties=["FCY_0", "FCYNAM_0"],
    )
    assert resolved.qualifiedTable() == "THBI.DIM_FACILITY"
    assert resolved.column("FCY_0") == "FCY_0"
    assert resolved.column("FCYNAM_0") == "FCYNAM_0"


# ---------------------------------------------------------------- Task 2 种子（交付函数）


@pytest.mark.asyncio
async def test_seed_facility_binding_creates_confirms_and_is_idempotent(
    dbSession, capsys
) -> None:
    """种子的真实交付行为：首次建 1 条并确认、再次 0 条、解析器读得到。

    只读 `FACILITY_BINDING` 配置的单元测试无法证明「建行 → 确认 → 可解析」
    （评审 Important #2）⇒ 这里用真实 PG 跑**交付函数本身**：
    首次 1 / 再次 0 钉幂等，最后一次 `resolveBinding` 钉「确实确认生效」。

    本体模型这里用真名（`stalePrefix=""`）：种子从本体模型复制物理表名/列名进绑定，
    `_assertSourceTable` 也要求 `ontology_class.source_table == "DIM_FACILITY"`。
    另种一个**无物理列**的属性 ⇒ 断言覆盖提示会如实报「可绑定 2/3」。
    """
    await _seedThbiDataSource(dbSession)
    classId, _propertyIdByName = await _seedClassWithProperties(
        dbSession,
        className=_FACILITY_CLASS,
        propertyColumns=_FACILITY_COLUMNS,
        stalePrefix="",
    )
    dbSession.add(
        OntologyProperty(
            class_id=classId,
            property_name="NO_COLUMN_PROPERTY",
            data_type="string",
            is_primary_key=False,
            is_foreign_key=False,
            source_column=None,
        )
    )
    await dbSession.commit()

    factory = dbModule.getSessionFactory()
    assert await seed_facility_binding(factory) == 1
    assert "属性可绑定 2/3" in capsys.readouterr().out
    assert await seed_facility_binding(factory) == 0

    # 规格 → 落库：审计 actor 必须来自规格（不是另一份字面量）。
    expectedActor = FACILITY_BINDING["confirmedBy"]
    sourceRows = (
        await dbSession.execute(
            select(ProcurementSourceBinding).where(
                ProcurementSourceBinding.schema_name == THBI_SCHEMA_NAME,
                ProcurementSourceBinding.source_table == _FACILITY_CLASS,
            )
        )
    ).scalars().all()
    assert [row.confirmed_by for row in sourceRows] == [expectedActor]
    propertyActors = (
        await dbSession.execute(
            select(ProcurementPropertyBinding.confirmed_by).where(
                ProcurementPropertyBinding.source_binding_id == sourceRows[0].id
            )
        )
    ).scalars().all()
    assert len(propertyActors) == 2
    assert set(propertyActors) == {expectedActor}

    resolved = await resolveBinding(
        dbSession,
        className=_FACILITY_CLASS,
        requiredProperties=["FCY_0", "FCYNAM_0"],
    )
    assert resolved.qualifiedTable() == "THBI.DIM_FACILITY"
    assert resolved.columns == {"FCY_0": "FCY_0", "FCYNAM_0": "FCYNAM_0"}
