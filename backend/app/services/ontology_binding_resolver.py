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
3. **候选不消费**：只认 `is_candidate IS FALSE` 且 `valid_to IS NULL` 的行。
4. **fail-closed**：类不存在/墓碑/无绑定/属性缺失/物理列缺失 ⇒ 抛
   `BindingResolutionError`（`ValidationError` 子类），绝不静默回退。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ValidationError
from app.domain.models import DataSource, OntologyClass, OntologyProperty
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)

#: 硬规则：唯一数据源（THBI Oracle）。
THBI_DATASOURCE_ID = 1
#: 硬规则：唯一 schema（Oracle owner）。
THBI_SCHEMA_NAME = "THBI"


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


#: 读路径取回的行形状：
#: (classId, className, validTo) / (sourceBindingId, datasourceId, schemaName,
#:  sourceTable, isCandidate, validTo) / (propertyId, propertyName,
#:  sourceColumn, classId)
BindingRows = Sequence[tuple]


def _flattenRows(rows: BindingRows) -> list[tuple]:
    """把「类行 / 源绑定行 / 属性行组」摊平成「一行一记录」。

    读路径给的是**扁平** list（每行一条记录）；单测给的是
    `(类行, 源绑定行, (属性行, ...))` 的分组形状。两种形状都摊平后再编译
    ⇒ 编译器只认「一行一记录」，不必猜调用方怎么分组。
    """
    flat: list[tuple] = []
    for row in rows:
        if not row:
            continue
        if all(isinstance(item, tuple) for item in row):
            flat.extend(item for item in row if item)
        else:
            flat.append(row)
    return flat


def _findClassRow(flatRows: list[tuple], className: str) -> tuple:
    """定位本体类行；找不到 ⇒ 类不存在（fail-closed）。"""
    for row in flatRows:
        if len(row) != 3:
            continue
        if isinstance(row[1], str) and row[1] == className:
            return row
    raise BindingResolutionError(f"本体类不存在：{className}")


def _findSourceRow(
    flatRows: list[tuple], *, className: str, datasourceId: int
) -> tuple:
    """定位唯一一条「已确认 + 生效中」的源绑定行。

    判据（与 `semantic_compiler_bindings` 同口径）：
    - `is_candidate` 必须**显式为假**（未知 ⇒ 不消费）；
    - `valid_to` 必须为空（已失效不是「当前生效的绑定」）；
    - 数据源必须匹配（硬规则：只有 THBI）。

    命中 0 条 ⇒ 无生效绑定；命中 ≥2 条 ⇒ 口径不唯一。两者都 fail-closed。
    """
    sourceRow = None
    for row in flatRows:
        if len(row) != 6:
            continue
        _, dsId, _, _, isCandidate, srcValidTo = row
        if dsId != datasourceId:
            continue
        if isCandidate is not False:
            continue
        if srcValidTo is not None:
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
    flatRows: list[tuple], *, classId: int | str, className: str
) -> dict[str, str]:
    """收集「本体属性名 → 物理列名」。

    物理列名只认**属性绑定**（`ProcurementPropertyBinding.source_column`）——
    `ontology_property.source_column` 是历史遗留列，允许与真实绑定不一致，
    解析器一律以绑定为准（硬规则：物理列名只从绑定读）。
    """
    columns: dict[str, str] = {}
    for row in flatRows:
        if len(row) != 4:
            continue
        _, propName, sourceColumn, propClassId = row
        if propClassId != classId:
            continue
        if not sourceColumn:
            raise BindingResolutionError(
                f"本体类 {className} 的属性绑定缺物理列名：{propName}"
            )
        columns[propName] = sourceColumn
    return columns


def _compileBinding(
    *, className: str, datasourceId: int, rows: BindingRows
) -> ResolvedBinding:
    """把读回的原始行编译成 `ResolvedBinding`（纯函数，便于单测）。

    fail-closed 判据（任一不满足即抛 `BindingResolutionError`）：
    - 类存在且未墓碑；
    - 该类在该数据源有**恰好一条**已确认 + 生效中的绑定；
    - 绑定的 schema 是 THBI（硬规则：一切数据对象出自 THBI）；
    - 候选绑定一律不消费。
    """
    flatRows = _flattenRows(rows)
    classRow = _findClassRow(flatRows, className)
    classId, _, validTo = classRow
    if validTo is not None:
        raise BindingResolutionError(f"本体类已删除（墓碑）：{className}")

    sourceRow = _findSourceRow(
        flatRows, className=className, datasourceId=datasourceId
    )
    _, _, schemaName, sourceTable, _, _ = sourceRow
    if schemaName != THBI_SCHEMA_NAME:
        raise BindingResolutionError(
            f"本体类 {className} 的绑定 schema 不是 {THBI_SCHEMA_NAME}：{schemaName}"
        )

    columns = _collectColumns(flatRows, classId=classId, className=className)
    return ResolvedBinding(
        className=className,
        datasourceId=datasourceId,
        schemaName=schemaName,
        sourceTable=sourceTable,
        columns=columns,
    )


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


async def _loadClassRow(session: AsyncSession, className: str) -> tuple | None:
    """读本体类行 `(id, class_name, valid_to)`；墓碑也读进来。

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
    return tuple(classRow)


async def _loadSourceRows(
    session: AsyncSession, *, classId: int, datasourceId: int
) -> list[tuple]:
    """读源绑定行 `(id, datasource_id, schema_name, source_table, is_candidate, valid_to)`。

    **不过滤** `is_candidate` / `valid_to` ⇒ 编译器才分得清
    「没有确认生效的绑定」与「多条生效绑定（口径不唯一）」。
    """
    sourceRows = (
        await session.execute(
            select(
                ProcurementSourceBinding.id,
                ProcurementSourceBinding.datasource_id,
                ProcurementSourceBinding.schema_name,
                ProcurementSourceBinding.source_table,
                ProcurementSourceBinding.is_candidate,
                ProcurementSourceBinding.valid_to,
            )
            .where(ProcurementSourceBinding.class_id == classId)
            .where(ProcurementSourceBinding.datasource_id == datasourceId)
        )
    ).all()
    return [tuple(row) for row in sourceRows]


async def _loadPropertyRows(
    session: AsyncSession, *, classId: int, datasourceId: int
) -> list[tuple]:
    """读属性绑定行 `(property_id, property_name, source_column, class_id)`。

    物理列名取**属性绑定**的 `source_column`（允许 ≠ 本体表的 `source_column`）。
    只认「已确认 + 生效中」且挂在**本数据源生效源绑定**下的属性行
    ⇒ 别处（别的数据源 / 候选源绑定）的列名不会漏进本类。
    """
    propertyRows = (
        await session.execute(
            select(
                OntologyProperty.id,
                OntologyProperty.property_name,
                ProcurementPropertyBinding.source_column,
                OntologyProperty.class_id,
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
            .where(ProcurementSourceBinding.is_candidate.is_(False))
            .where(ProcurementSourceBinding.valid_to.is_(None))
            .where(ProcurementPropertyBinding.is_candidate.is_(False))
            .where(ProcurementPropertyBinding.valid_to.is_(None))
        )
    ).all()
    return [tuple(row) for row in propertyRows]


async def _loadBindingRows(
    session: AsyncSession, *, className: str, datasourceId: int
) -> list[tuple]:
    """一次性读回类 / 源绑定 / 属性绑定三类行（供 `_compileBinding` 编译）。

    三类行的形状见 `BindingRows`。类不存在 ⇒ 空列表（编译器报「本体类不存在」）。
    """
    classRow = await _loadClassRow(session, className)
    if classRow is None:
        return []
    classId = classRow[0]
    rows: list[tuple] = [classRow]
    rows.extend(await _loadSourceRows(session, classId=classId, datasourceId=datasourceId))
    rows.extend(
        await _loadPropertyRows(session, classId=classId, datasourceId=datasourceId)
    )
    return rows


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
    rows = await _loadBindingRows(
        session, className=className, datasourceId=datasourceId
    )
    resolved = _compileBinding(
        className=className, datasourceId=datasourceId, rows=rows
    )
    missing = [name for name in requiredProperties if name not in resolved.columns]
    if missing:
        raise BindingResolutionError(
            f"本体类 {className} 缺少必需属性的绑定：{missing}"
            f"（已绑定：{sorted(resolved.columns)}）"
        )
    return resolved
