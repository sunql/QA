"""采购本体绑定种子（2026-11-25，P5 到货跟踪）。

背景
----
`DIM_FACILITY`（基地字典）本体类**已存在**（117 属性，含 `FCY_0` / `FCYNAM_0`），
但 `procurement_source_binding` 里没有它的行 ⇒ 运行时解析器解析不到物理表
⇒ 待到货的 `siteKeys` 过滤拿不到基地名。

本脚本补这条绑定，是 P5 **唯一新增的本体资产**。

写路径走 **service 层**（`ProcurementBindingService`）而不是裸 INSERT
------------------------------------------------------------------
1. **版本冻结点**：service 强制「候选 → 确认」的顺序（先确认全部属性绑定，
   再确认源绑定）⇒ 生效视图不会出现「父已确认、子还是候选」的空壳映射；
   裸 INSERT 直接落 `is_candidate=false` 会绕过这条纪律。
2. **留痕**：每次写入都落 `audit_log`（治理动作必须可反查）。
3. **闸门**：属性必须属于本源绑定的类、复合键序号从 1 起连续 ⇒ service 有闸门。

⚠️ **列映射只能来自属性绑定**：`ontology_binding_resolver` 的物理列名**只**认
`procurement_property_binding.source_column`（本体表里的 `source_column` 是历史遗留列）
⇒ 只补源绑定行的话，解析器能拿到表名但**拿不到任何列** ⇒ `requiredProperties`
（`FCY_0` / `FCYNAM_0`）必然 fail-closed ⇒ 本脚本**同时**补属性绑定。

物理表名 / 物理列名**不写死在脚本里**：表名取自本体模型
（`ontology_class.source_table`），列名取自 `ontology_property.source_column`
（两者都是本体发现的结果）⇒ 脚本里只有一份真相，漂移时响亮失败。

幂等：按 (class_id, datasource_id, schema_name, source_table, binding_role)
预查生效行；已存在且已确认 ⇒ 不覆盖既有确认信息，直接返回 0。
若上次运行中断留下**候选**行 ⇒ 复用该行续跑（补属性 → 确认属性 → 确认源绑定）。

⚠️ **已确认的绑定不会补属性**（重跑不追平本体）：service 把「已确认源绑定」的
子集合视为**冻结**（`createPropertyBinding` 对已确认父行直接拒绝）⇒ 若 `DIM_FACILITY`
后续新增了属性，重跑本脚本**不会**把它们补进来，必须先失效旧绑定 / 建新版本
（业务裁决后另行发布）。因此返回 0 只表示「源绑定已确认生效」，
**不**表示「属性集合与本体一致」。

用法（backend/ 下）：
    python -m scripts.seed_procurement_bindings
"""

from __future__ import annotations

import asyncio
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import getSettings
from app.domain.models import DataSource, OntologyClass, OntologyProperty
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)
from app.schemas.procurement_binding import (
    PropertyBindingCreate,
    SourceBindingCreate,
)
from app.services.procurement_binding_service import ProcurementBindingService


class FacilityBindingSpec(TypedDict):
    """源绑定规格的键契约（`TypedDict` ⇒ 键名笔误在静态检查期就暴露）。

    **每个键都必须驱动行为**：`className` / `datasourceId` / `schemaName` /
    `sourceTable` / `bindingRole` 进绑定的唯一键与取值；`confirmedBy` 是审计 actor
    （调用点直接读本字典，**不设**第二份 actor 字面量 ⇒ 改规格即改留痕）；
    `isCandidate` 是**期望终态**（`False` = 已确认生效），由 `_assertTerminalState`
    拿实际落库行与之比对 ⇒ 流程没走到预期终态时响亮失败（不是死字段）。
    """

    className: str
    datasourceId: int
    schemaName: str
    sourceTable: str
    bindingRole: str
    isCandidate: bool
    confirmedBy: str


#: `DIM_FACILITY` 的源绑定规格（THBI 硬规则：数据源 id=1 + schema THBI + BASE 角色）。
FACILITY_BINDING: FacilityBindingSpec = {
    "className": "DIM_FACILITY",
    "datasourceId": 1,
    "schemaName": "THBI",
    "sourceTable": "DIM_FACILITY",
    "bindingRole": "BASE",
    "isCandidate": False,
    "confirmedBy": "m1-b-confirm",
}


async def _findClassRow(session: AsyncSession) -> OntologyClass:
    """取生效中的本体类行（未墓碑 + 版本最高）。

    口径与 `ontology_binding_resolver._loadClassRow` 一致（读路径与写路径必须
    指向**同一行** ⇒ 否则解析器读到的类与绑定的类对不上）。
    """
    rows = (
        await session.execute(
            select(OntologyClass)
            .where(
                OntologyClass.class_name == FACILITY_BINDING["className"],
                OntologyClass.valid_to.is_(None),
            )
            .order_by(OntologyClass.version.desc())
        )
    ).scalars().all()
    if not rows:
        raise RuntimeError(
            f"本体类不存在或已墓碑：{FACILITY_BINDING['className']}（先跑 seed_ontology）"
        )
    return rows[0]


def _assertSourceTable(classRow: OntologyClass) -> None:
    """本体模型声明的物理表必须与脚本规格一致（不一致 ⇒ 响亮失败，人工裁决）。

    为什么要有这道闸：物理表名有两处可能的真相（本体模型 vs 脚本规格）⇒
    不一致时静默取其一 = 把漂移埋进数据里。
    """
    declared = str(classRow.source_table or "")
    if declared != str(FACILITY_BINDING["sourceTable"]):
        raise RuntimeError(
            f"本体类 {classRow.class_name} 的 source_table 与绑定规格不一致："
            f"本体={declared!r} 规格={FACILITY_BINDING['sourceTable']!r}"
        )


async def _assertDatasource(session: AsyncSession) -> None:
    """数据源必须存在且启用（否则绑定了也读不出来）。"""
    datasourceId = int(FACILITY_BINDING["datasourceId"])
    datasource = await session.get(DataSource, datasourceId)
    if datasource is None:
        raise RuntimeError(f"数据源不存在：{datasourceId}")
    if not datasource.is_active:
        raise RuntimeError(f"数据源未启用：{datasourceId}")


async def _findActiveSourceBinding(
    session: AsyncSession, classId: int, sourceTable: str
) -> ProcurementSourceBinding | None:
    """按唯一键（class + ds + schema + table + role）找**生效**源绑定行。"""
    return (
        await session.execute(
            select(ProcurementSourceBinding).where(
                ProcurementSourceBinding.class_id == classId,
                ProcurementSourceBinding.datasource_id == FACILITY_BINDING["datasourceId"],
                ProcurementSourceBinding.schema_name == FACILITY_BINDING["schemaName"],
                ProcurementSourceBinding.source_table == sourceTable,
                ProcurementSourceBinding.binding_role == FACILITY_BINDING["bindingRole"],
                ProcurementSourceBinding.valid_to.is_(None),
            )
        )
    ).scalars().first()


async def _createSourceBinding(
    session: AsyncSession,
    service: ProcurementBindingService,
    classRow: OntologyClass,
) -> ProcurementSourceBinding:
    """建**候选**源绑定（物理表名取自本体模型，不是脚本常量）。"""
    return await service.createSourceBinding(
        session,
        SourceBindingCreate(
            class_id=classRow.id,
            datasource_id=int(FACILITY_BINDING["datasourceId"]),
            schema_name=str(FACILITY_BINDING["schemaName"]),
            source_table=str(classRow.source_table),
            binding_role=str(FACILITY_BINDING["bindingRole"]),
            filter_expression=None,
            filter_allowlist=[],
        ),
        actor=FACILITY_BINDING["confirmedBy"],
    )


async def _loadBindableProperties(
    session: AsyncSession, classId: int
) -> list[OntologyProperty]:
    """取本类所有「有物理列」的属性（按属性名排序）。

    物理列名取自 `ontology_property.source_column`（本体模型的发现结果）⇒
    脚本里不出现任何物理列名字面量。
    """
    return list(
        (
            await session.execute(
                select(OntologyProperty)
                .where(
                    OntologyProperty.class_id == classId,
                    OntologyProperty.source_column.is_not(None),
                )
                .order_by(OntologyProperty.property_name)
            )
        ).scalars().all()
    )


async def _propertyCoverage(
    session: AsyncSession, classId: int
) -> tuple[int, int]:
    """返回 `(属性总数, 可绑定属性数)`。

    两者不等 ⇒ 有属性缺物理列（`source_column IS NULL`），**无法绑定**：
    运行时解析器对它 fail-closed（不会静默错列），但种子若只报「新建 N 条」
    就看不出这个缺口 ⇒ 调用方据此打一行覆盖提示。
    """
    total = (
        await session.execute(
            select(func.count())
            .select_from(OntologyProperty)
            .where(OntologyProperty.class_id == classId)
        )
    ).scalar_one()
    bindable = (
        await session.execute(
            select(func.count())
            .select_from(OntologyProperty)
            .where(
                OntologyProperty.class_id == classId,
                OntologyProperty.source_column.is_not(None),
            )
        )
    ).scalar_one()
    return int(total), int(bindable)


def _creationOrder(
    properties: list[OntologyProperty],
    keyOrdinalByPropId: dict[int, int],
    existingPropIds: set[int],
) -> list[OntologyProperty]:
    """排出待建属性的顺序：**键列在前**（按 `key_ordinal` 升序），非键列在后。

    为什么键列必须升序：service 在建行时就校验「序号前缀齐全」
    （`_assertKeyOrdinalFree`）⇒ 先建 `key_ordinal=3` 会当场撞闸。
    非键列不占序号（`key_ordinal=None`，闸门直接放行），排在键列之后即可。
    """
    return sorted(
        (prop for prop in properties if prop.id not in existingPropIds),
        key=lambda prop: (
            keyOrdinalByPropId.get(prop.id) is None,
            keyOrdinalByPropId.get(prop.id) or 0,
            prop.property_name,
        ),
    )


async def _ensurePropertyBindings(
    session: AsyncSession,
    service: ProcurementBindingService,
    sourceBindingId: int,
    classId: int,
) -> int:
    """补齐属性绑定，返回新建条数。

    主键属性按 `property_name` 序标 `key_ordinal` 1..N（复合键序号必须连续）
    ⇒ 建行顺序由 `_creationOrder` 决定（键列在前、序号升序）。
    """
    properties = await _loadBindableProperties(session, classId)
    keyOrdinalByPropId = {
        prop.id: ordinal + 1
        for ordinal, prop in enumerate(
            [item for item in properties if item.is_primary_key]
        )
    }
    existingPropIds = set(
        (
            await session.execute(
                select(ProcurementPropertyBinding.property_id).where(
                    ProcurementPropertyBinding.source_binding_id == sourceBindingId,
                    ProcurementPropertyBinding.valid_to.is_(None),
                )
            )
        ).scalars().all()
    )
    ordered = _creationOrder(properties, keyOrdinalByPropId, existingPropIds)
    created = 0
    for prop in ordered:
        await service.createPropertyBinding(
            session,
            sourceBindingId,
            PropertyBindingCreate(
                property_id=prop.id,
                source_column=str(prop.source_column),
                key_ordinal=keyOrdinalByPropId.get(prop.id),
                transform_expression=None,
            ),
            actor=FACILITY_BINDING["confirmedBy"],
        )
        created += 1
    return created


async def _confirmBindings(
    session: AsyncSession,
    service: ProcurementBindingService,
    sourceBindingId: int,
) -> None:
    """确认全部属性绑定，再确认源绑定（service 强制的顺序，不可颠倒）。"""
    pending = (
        await session.execute(
            select(ProcurementPropertyBinding).where(
                ProcurementPropertyBinding.source_binding_id == sourceBindingId,
                ProcurementPropertyBinding.valid_to.is_(None),
                ProcurementPropertyBinding.is_candidate.is_(True),
            )
        )
    ).scalars().all()
    for binding in pending:
        await service.confirmPropertyBinding(
            session, sourceBindingId, binding.id, actor=FACILITY_BINDING["confirmedBy"]
        )
    source = await service.getSourceBinding(session, sourceBindingId)
    if source.is_candidate:
        await service.confirmSourceBinding(
            session, sourceBindingId, actor=FACILITY_BINDING["confirmedBy"]
        )


async def _assertTerminalState(session: AsyncSession, sourceBindingId: int) -> None:
    """校验种子结束后的**目标终态**：实际落库行与规格期望一致（已确认、未失效）。

    期望值取自 `FACILITY_BINDING["isCandidate"]`（契约：已确认 = `False`）——
    拿**实际状态**与规格比对，而不是把期望再写一遍字面量 ⇒ 规格字段真正被消费。

    为什么必须校验：解析器只消费 `is_candidate IS FALSE` 且 `valid_to IS NULL` 的行
    ⇒ 若确认链路哪天被短路，「新建 1 条」会写进去但**永远读不到**（静默失效）。
    """
    source = await session.get(
        ProcurementSourceBinding, sourceBindingId, populate_existing=True
    )
    if source is None or source.valid_to is not None:
        raise RuntimeError(
            f"源绑定 id={sourceBindingId} 已失效或不存在"
            f"（valid_to={None if source is None else source.valid_to}）"
        )
    if source.is_candidate != FACILITY_BINDING["isCandidate"]:
        raise RuntimeError(
            f"源绑定 id={sourceBindingId} 的 is_candidate={source.is_candidate} "
            f"与规格期望 {FACILITY_BINDING['isCandidate']} 不一致"
        )


async def seed_facility_binding(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """幂等补 `DIM_FACILITY` 的源绑定 + 属性绑定。

    返回**新建的源绑定行数**（0 = 已存在且已确认）。
    已确认的绑定**不会**补新属性（见模块 docstring 的冻结说明）。
    """
    service = ProcurementBindingService()
    async with session_factory() as session:
        classRow = await _findClassRow(session)
        _assertSourceTable(classRow)
        await _assertDatasource(session)
        totalProperties, bindableProperties = await _propertyCoverage(
            session, classRow.id
        )
        if bindableProperties != totalProperties:
            print(
                f"seed_procurement_bindings: ⚠️ {classRow.class_name} 属性可绑定 "
                f"{bindableProperties}/{totalProperties}"
                "（其余 source_column IS NULL，无法绑定）"
            )
        sourceTable = str(classRow.source_table)
        existing = await _findActiveSourceBinding(session, classRow.id, sourceTable)
        if existing is not None and not existing.is_candidate:
            return 0
        source = existing or await _createSourceBinding(session, service, classRow)
        await _ensurePropertyBindings(session, service, source.id, classRow.id)
        await _confirmBindings(session, service, source.id)
        await _assertTerminalState(session, source.id)
        return 0 if existing is not None else 1


async def main() -> None:
    """CLI 入口：独立引擎跑种子，跑完释放连接池。"""
    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        created = await seed_facility_binding(factory)
        print(f"seed_procurement_bindings: 新建 {created} 条（DIM_FACILITY 绑定）")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
