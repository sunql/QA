"""运行时本体绑定解析（2026-11-25，P5 到货跟踪）。

为什么不用 `semantic_compiler_bindings.compileSourceBindings()`
-----------------------------------------------------------
那个是**纯函数快照编译器**：无 IO、需 6 个查找字典、只产语义发布快照。
工具运行时要的是「按需取一条绑定 + 校验 + 立刻用」，故本模块提供读路径。

硬规则（2026-11-25 用户明确）
------------------------------
1. **一切数据对象出自 THBI**：默认 `datasource_id=1` + `schema_name=THBI`；
   调用方不得改数据源。
2. **本体对接，不硬编码**：物理表名/列名只从绑定读，不进代码常量。
3. **只消费已确认生效行**：`is_candidate IS FALSE` 且 `valid_to IS NULL`
   且 `valid_from <= now()`（与 `semantic_compiler_bindings` 的发布口径一致）。
4. **fail-closed**：类不存在/墓碑/无生效绑定/属性缺失/物理列缺失 ⇒ 抛
   `BindingResolutionError`（`ValidationError` 子类），绝不静默回退。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ValidationError
from app.domain.models import DataSource, OntologyClass, OntologyProperty
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)
from app.services.ontology_binding_rows import (
    BindingRows,
    ClassRow,
    PropertyBindingRow,
    SourceBindingRow,
)

#: 硬规则：唯一数据源（THBI Oracle）。
THBI_DATASOURCE_ID = 1
#: 硬规则：唯一 schema（Oracle owner）。
THBI_SCHEMA_NAME = "THBI"
#: 本读路径只消费 **BASE** 角色的绑定。
#:
#: 绑定表允许同一类在同一数据源下有多条生效绑定（唯一索引含 `binding_role`），
#: 但那些是**度量口径**的分角色绑定（NUMERATOR / DENOMINATOR / FILTER），
#: 归语义发布编译器按角色处理，不属于「类 → 物理表」这条读路径。
#: ⇒ 读路径只认 BASE；唯一性检查也**在 BASE 角色范围内**做。
BINDING_ROLE_BASE = "BASE"


class BindingResolutionError(ValidationError):
    """本体绑定解析失败（fail-closed，禁止回退硬编码表）。"""


@dataclass(frozen=True, slots=True)
class ResolvedBinding:
    """一条已解析的本体类 → 物理表绑定。"""

    className: str
    datasourceId: int
    schemaName: str
    sourceTable: str
    columns: Mapping[str, str]

    def __post_init__(self) -> None:
        """把列映射冻结成只读视图（`frozen=True` 挡不住 dict 内部被改）。"""
        object.__setattr__(self, "columns", MappingProxyType(dict(self.columns)))

    def qualifiedTable(self) -> str:
        """限定物理表名（schema.table）。"""
        return f"{self.schemaName}.{self.sourceTable}"

    def column(self, propertyName: str) -> str:
        """属性名 → 物理列名；未知属性 fail-closed。"""
        sourceColumn = self.columns.get(propertyName)
        if not sourceColumn:
            raise BindingResolutionError(
                f"本体类 {self.className} 的绑定里没有属性 {propertyName}"
                f"（已绑定属性：{sorted(self.columns)}）"
            )
        return sourceColumn


# ---------------------------------------------------------------- 纯编译层


def _isEffective(validFrom: datetime | None, now: datetime) -> bool:
    """绑定是否**已生效**：`valid_from` 不晚于此刻。

    与 `semantic_compiler_bindings._isEffectiveBinding` 同口径：库里
    `valid_from` 是 NOT NULL DEFAULT now()，None 只可能是尚未落库的对象
    ⇒ 按「已生效」处理（落库后必然有值）。
    """
    if validFrom is None:
        return True
    return validFrom <= now


def _findSourceRow(
    sourceRows: Sequence[SourceBindingRow],
    *,
    className: str,
    datasourceId: int,
    now: datetime,
) -> SourceBindingRow:
    """定位唯一一条「已确认 + 生效中 + BASE」的源绑定行。

    判据：数据源匹配、角色是 BASE、`is_candidate` **显式为假**（未知 ⇒ 不消费）、
    `valid_to` 为空、`valid_from` 不晚于此刻。

    命中 0 条 ⇒ 无生效绑定；命中 ≥2 条 ⇒ 口径不唯一。两者都 fail-closed。
    """
    sourceRow = None
    for row in sourceRows:
        if row.datasourceId != datasourceId:
            continue
        if row.bindingRole != BINDING_ROLE_BASE:
            continue
        if row.isCandidate is not False:
            continue
        if row.validTo is not None:
            continue
        if not _isEffective(row.validFrom, now):
            continue
        if sourceRow is not None:
            raise BindingResolutionError(
                f"本体类 {className} 在数据源 {datasourceId} 有多条生效绑定，口径不唯一"
            )
        sourceRow = row
    if sourceRow is None:
        raise BindingResolutionError(
            f"本体类 {className} 在数据源 {datasourceId} 没有已确认生效的绑定"
            f"（候选绑定不可消费）"
        )
    return sourceRow


def _collectColumns(
    propertyRows: Sequence[PropertyBindingRow],
    *,
    classId: int,
    sourceBindingId: int,
    className: str,
    now: datetime,
) -> dict[str, str]:
    """收集「本体属性名 → 物理列名」。

    只收**属于本类且挂在已解析的那条源绑定上**的属性行 ⇒ 别的角色
    （FILTER / NUMERATOR / …）或别的源绑定的列名不会混进来。
    物理列名只认**属性绑定**的 `source_column` —— `ontology_property.source_column`
    是历史遗留列，允许与真实绑定不一致，解析器一律以绑定为准。
    """
    columns: dict[str, str] = {}
    for row in propertyRows:
        if row.classId != classId:
            continue
        if row.sourceBindingId != sourceBindingId:
            continue
        if not _isEffective(row.validFrom, now):
            continue
        if not row.sourceColumn:
            raise BindingResolutionError(
                f"本体类 {className} 的属性绑定缺物理列名：{row.propertyName}"
            )
        columns[row.propertyName] = row.sourceColumn
    return columns


def _compileBinding(
    *,
    className: str,
    datasourceId: int,
    classRow: ClassRow | None,
    sourceRows: Sequence[SourceBindingRow],
    propertyRows: Sequence[PropertyBindingRow],
    now: datetime | None = None,
) -> ResolvedBinding:
    """把读回的原始行编译成 `ResolvedBinding`（纯函数，便于单测）。

    fail-closed 判据（任一不满足即抛 `BindingResolutionError`）：
    - 类存在且未墓碑；
    - 该类在该数据源有**恰好一条**已确认 + 生效中 + BASE 的绑定；
    - 绑定的 schema 是 THBI（硬规则：一切数据对象出自 THBI）。

    :param now: 生效时刻基准（默认当前时间）；单测可注入以构造未来生效行。
    """
    effectiveNow = now if now is not None else datetime.now(UTC)
    if classRow is None:
        raise BindingResolutionError(f"本体类不存在：{className}")
    if classRow.validTo is not None:
        raise BindingResolutionError(f"本体类已删除（墓碑）：{className}")

    sourceRow = _findSourceRow(
        sourceRows, className=className, datasourceId=datasourceId, now=effectiveNow
    )
    if sourceRow.schemaName != THBI_SCHEMA_NAME:
        raise BindingResolutionError(
            f"本体类 {className} 的绑定 schema 不是 {THBI_SCHEMA_NAME}："
            f"{sourceRow.schemaName}"
        )

    columns = _collectColumns(
        propertyRows,
        classId=classRow.classId,
        sourceBindingId=sourceRow.sourceBindingId,
        className=className,
        now=effectiveNow,
    )
    return ResolvedBinding(
        className=className,
        datasourceId=datasourceId,
        schemaName=sourceRow.schemaName,
        sourceTable=sourceRow.sourceTable,
        columns=columns,
    )


# ---------------------------------------------------------------- 读路径（IO）


async def _assertThbiDatasource(session: AsyncSession, datasourceId: int) -> None:
    """数据源硬闸：只认 THBI（id=1），且该数据源必须存在 + 启用。

    硬规则「调用方不得改数据源」的落点：参数保留只为把口径写显式，
    越界即 fail-closed（而不是悄悄去查另一个库）。
    """
    if datasourceId != THBI_DATASOURCE_ID:
        raise BindingResolutionError(
            f"数据源硬闸：只允许 THBI（datasource_id={THBI_DATASOURCE_ID}），"
            f"收到 {datasourceId}"
        )
    dataSourceRow = (
        await session.execute(
            select(DataSource.id, DataSource.is_active).where(
                DataSource.id == datasourceId
            )
        )
    ).first()
    if dataSourceRow is None:
        raise BindingResolutionError(f"数据源不存在：{datasourceId}")
    if dataSourceRow[1] is not True:
        raise BindingResolutionError(f"数据源未启用：{datasourceId}")


async def _loadClassRow(session: AsyncSession, className: str) -> ClassRow | None:
    """读本体类行；墓碑也读进来。

    墓碑必须读进来 ⇒ 报「已删除（墓碑）」而不是「不存在」（两种病因不同）。
    现行版本优先（valid_to IS NULL），其次版本号高者优先 ⇒ 读路径确定。
    """
    classRow = (
        await session.execute(
            select(
                OntologyClass.id, OntologyClass.class_name, OntologyClass.valid_to
            )
            .where(OntologyClass.class_name == className)
            .order_by(
                OntologyClass.valid_to.is_(None).desc(), OntologyClass.version.desc()
            )
        )
    ).first()
    if classRow is None:
        return None
    return ClassRow(classRow[0], classRow[1], classRow[2])


async def _loadSourceRows(
    session: AsyncSession, *, classId: int, datasourceId: int
) -> tuple[SourceBindingRow, ...]:
    """读源绑定行（只取 BASE 角色、已确认、已生效）。

    `valid_from <= now()` 在**查询里**过滤（未来才生效的绑定不进读路径）；
    `valid_to` / `is_candidate` **不过滤** ⇒ 编译器才分得清
    「没有确认生效的绑定」与「多条生效绑定（口径不唯一）」。
    """
    sourceRows = (
        await session.execute(
            select(
                ProcurementSourceBinding.id,
                ProcurementSourceBinding.datasource_id,
                ProcurementSourceBinding.schema_name,
                ProcurementSourceBinding.source_table,
                ProcurementSourceBinding.binding_role,
                ProcurementSourceBinding.is_candidate,
                ProcurementSourceBinding.valid_to,
                ProcurementSourceBinding.valid_from,
            )
            .where(ProcurementSourceBinding.class_id == classId)
            .where(ProcurementSourceBinding.datasource_id == datasourceId)
            .where(ProcurementSourceBinding.binding_role == BINDING_ROLE_BASE)
            .where(ProcurementSourceBinding.valid_from <= func.now())
        )
    ).all()
    return tuple(
        SourceBindingRow(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]
        )
        for row in sourceRows
    )


async def _loadPropertyRows(
    session: AsyncSession, *, classId: int, datasourceId: int
) -> tuple[PropertyBindingRow, ...]:
    """读属性绑定行（只取本类 + 本数据源 + BASE 源绑定下的已确认生效行）。

    物理列名取**属性绑定**的 `source_column`（允许 ≠ 本体表的 `source_column`）。
    join 到源绑定 ⇒ 只有**那一条 BASE 绑定**的属性列会进来，
    别的角色（FILTER / NUMERATOR / …）的表列不会混进本类的列映射。
    """
    propertyRows = (
        await session.execute(
            select(
                OntologyProperty.id,
                OntologyProperty.property_name,
                ProcurementPropertyBinding.source_column,
                OntologyProperty.class_id,
                ProcurementPropertyBinding.source_binding_id,
                ProcurementPropertyBinding.valid_from,
            )
            .join(
                ProcurementPropertyBinding,
                ProcurementPropertyBinding.property_id == OntologyProperty.id,
            )
            .join(
                ProcurementSourceBinding,
                ProcurementSourceBinding.id
                == ProcurementPropertyBinding.source_binding_id,
            )
            .where(OntologyProperty.class_id == classId)
            .where(ProcurementSourceBinding.class_id == classId)
            .where(ProcurementSourceBinding.datasource_id == datasourceId)
            .where(ProcurementSourceBinding.binding_role == BINDING_ROLE_BASE)
            .where(ProcurementSourceBinding.is_candidate.is_(False))
            .where(ProcurementSourceBinding.valid_to.is_(None))
            .where(ProcurementSourceBinding.valid_from <= func.now())
            .where(ProcurementPropertyBinding.is_candidate.is_(False))
            .where(ProcurementPropertyBinding.valid_to.is_(None))
            .where(ProcurementPropertyBinding.valid_from <= func.now())
        )
    ).all()
    return tuple(
        PropertyBindingRow(row[0], row[1], row[2], row[3], row[4], row[5])
        for row in propertyRows
    )


async def _loadBindingRows(
    session: AsyncSession, *, className: str, datasourceId: int
) -> BindingRows:
    """一次性读回类 / 源绑定 / 属性绑定三类行（供 `_compileBinding` 编译）。

    三类行分三个序列返回（不是「一个扁平 list 靠长度猜」）⇒ 编译层拿到的
    每一行都是**类型正确**的行。类不存在 ⇒ `classRow=None`。
    """
    classRow = await _loadClassRow(session, className)
    if classRow is None:
        return None, (), ()
    sourceRows = await _loadSourceRows(
        session, classId=classRow.classId, datasourceId=datasourceId
    )
    propertyRows = await _loadPropertyRows(
        session, classId=classRow.classId, datasourceId=datasourceId
    )
    return classRow, sourceRows, propertyRows


async def resolveBinding(
    session: AsyncSession,
    *,
    className: str,
    requiredProperties: Sequence[str],
    datasourceId: int = THBI_DATASOURCE_ID,
) -> ResolvedBinding:
    """解析本体类的生效绑定，并校验必需属性都已绑定。

    :raises BindingResolutionError: 类不存在/墓碑/无生效绑定/属性缺失/数据源越界。
    """
    await _assertThbiDatasource(session, datasourceId)
    classRow, sourceRows, propertyRows = await _loadBindingRows(
        session, className=className, datasourceId=datasourceId
    )
    resolved = _compileBinding(
        className=className,
        datasourceId=datasourceId,
        classRow=classRow,
        sourceRows=sourceRows,
        propertyRows=propertyRows,
    )
    missing = [name for name in requiredProperties if name not in resolved.columns]
    if missing:
        raise BindingResolutionError(
            f"本体类 {className} 缺少必需属性的绑定：{missing}"
            f"（已绑定：{sorted(resolved.columns)}）"
        )
    return resolved
