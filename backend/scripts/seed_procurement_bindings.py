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
（`FCY_0` / `FCNAM_0`）必然 fail-closed ⇒ 本脚本**同时**补属性绑定。

物理表名 / 物理列名**不写死在脚本里**：表名取自本体模型
（`ontology_class.source_table`），列名取自 `ontology_property.source_column`
（两者都是本体发现的结果）⇒ 脚本里只有一份真相，漂移时响亮失败。

幂等：按 (class_id, datasource_id, schema_name, source_table, binding_role)
预查生效行；已存在且已确认 ⇒ 不覆盖既有确认信息，直接返回 0。
若上次运行中断留下**候选**行 ⇒ 复用该行续跑（补属性 → 确认属性 → 确认源绑定）。

用法（backend/ 下）：
    python -m scripts.seed_procurement_bindings
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
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

#: `DIM_FACILITY` 的源绑定规格（THBI 硬规则：数据源 id=1 + schema THBI + BASE 角色）。
#:
#: `isCandidate=False` 描述的是**目标终态**（已确认生效，不是候选）；实际写入走
#: service 的「候选 → 确认」两段式（见模块 docstring）。
FACILITY_BINDING: dict[str, Any] = {
    "className": "DIM_FACILITY",
    "datasourceId": 1,
    "schemaName": "THBI",
    "sourceTable": "DIM_FACILITY",
    "bindingRole": "BASE",
    "isCandidate": False,
    "confirmedBy": "m1-b-confirm",
}

#: 确认人（审计口径）——与仓内既有 9 条采购绑定一致。
ACTOR = "m1-b-confirm"


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
        actor=ACTOR,
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


async def _ensurePropertyBindings(
    session: AsyncSession,
    service: ProcurementBindingService,
    sourceBindingId: int,
    classId: int,
) -> int:
    """补齐属性绑定，返回新建条数。

    主键属性按 `property_name` 序标 `key_ordinal` 1..N（复合键序号必须连续）。
    **建键列的顺序**：先建 `key_ordinal=1`，再建 2、3… ⇒ service 的「序号前缀齐全」
    闸门才放行（乱序建 ⇒ 撞闸）。
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
    ordered = sorted(
        (prop for prop in properties if prop.id not in existingPropIds),
        key=lambda prop: (
            keyOrdinalByPropId.get(prop.id, 0),
            prop.property_name,
        ),
    )
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
            actor=ACTOR,
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
            session, sourceBindingId, binding.id, actor=ACTOR
        )
    source = await service.getSourceBinding(session, sourceBindingId)
    if source.is_candidate:
        await service.confirmSourceBinding(session, sourceBindingId, actor=ACTOR)


async def seed_facility_binding(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """幂等补 `DIM_FACILITY` 的源绑定 + 属性绑定。

    返回**新建的源绑定行数**（0 = 已存在且已确认）。
    """
    service = ProcurementBindingService()
    async with session_factory() as session:
        classRow = await _findClassRow(session)
        _assertSourceTable(classRow)
        await _assertDatasource(session)
        sourceTable = str(classRow.source_table)
        existing = await _findActiveSourceBinding(session, classRow.id, sourceTable)
        if existing is not None and not existing.is_candidate:
            return 0
        source = existing or await _createSourceBinding(session, service, classRow)
        await _ensurePropertyBindings(session, service, source.id, classRow.id)
        await _confirmBindings(session, service, source.id)
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
