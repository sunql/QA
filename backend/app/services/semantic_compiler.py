"""语义编译器：治理表 → 自包含 Manifest（P1a / D5）。

为什么需要编译器（裁决一）：本体治理表是**原地 UPDATE**（Phase 6 已移除版本管理），
所以「发布时的本体长什么样」无法事后从表里还原 —— 必须在发布那一刻整份快照下来。
本模块负责把活跃本体编译成一份**与实例无关、可稳定序列化**的 Manifest 字典。

设计要点：

1. **纯核心 + 薄加载层**：`compileFromRows` 不碰数据库（单测直接构造行），
   `compileManifest(session)` 只负责取数。核心因此可以用行对象/列元组等鸭子类型输入。
2. **确定性**：类按 `className`、属性按 `propertyName`、JOIN 按 `joinKey` 稳定排序；
   字段全部显式出现（标量缺省 `null`，列表缺省 `[]`）。同样的治理内容换一个环境
   （测试库 / 生产）必须得到同样的字节 —— Manifest 里**不出现**自增 id、created_by、
   version/valid_* 这些依赖实例的残留列。
3. **活跃模型语义**：`valid_to` 非空是软删除墓碑。墓碑类、其属性、以及任一端点是墓碑的
   JOIN 都不属于活跃模型 ⇒ 编译时过滤掉（实测本地库有 18 个墓碑类、463 条墓碑属性、
   72 条端点已删的 JOIN；不过滤就会把已删除的类重新发布出去）。
4. **悬空引用**：指向墓碑类的 `refClassName` / `parentClassName` 编译为 `null`
   （目标已不在模型中）；指向**完全不存在**的 class_id 说明数据被写坏 ⇒ 抛 `ValueError`。
5. **不静默降级**：同名多版本活跃类（`uq_ontology_class_name_version` 只约束 name+version，
   Phase 6 后同名多行属异常）直接抛错，而不是挑一行发布。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty

SCHEMA_VERSION = "v1"
"""Manifest 自身 Schema 版本。与发布 version 解耦：Schema 演进后旧快照仍可回放。"""


def compileFromRows(
    *,
    classes: Iterable[OntologyClass],
    properties: Iterable[OntologyProperty],
    joins: Iterable[OntologyJoin],
) -> dict[str, Any]:
    """把治理表行编译成 Manifest 字典（纯函数，无 IO）。"""
    allClasses = list(classes)
    allClassById = {row.id: row for row in allClasses}
    activeClasses = _sortBy(
        (row for row in allClasses if row.valid_to is None),
        key=lambda row: row.class_name,
    )
    _rejectDuplicateNames(activeClasses)
    activeNameById = {row.id: row.class_name for row in activeClasses}
    propsByClassId = _groupActiveProperties(properties, allClassById)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "classes": [
            _compileClass(row, allClassById, propsByClassId.get(row.id, ()))
            for row in activeClasses
        ],
        "joins": _compileJoins(joins, allClassById, activeNameById),
    }


async def compileManifest(session: AsyncSession) -> dict[str, Any]:
    """从真实治理表取数并编译（发布/校验时调用，非高频路径）。"""
    classes = (await session.execute(select(OntologyClass))).scalars().all()
    properties = (await session.execute(select(OntologyProperty))).scalars().all()
    joins = (await session.execute(select(OntologyJoin))).scalars().all()
    return compileFromRows(classes=classes, properties=properties, joins=joins)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _sortBy(rows: Iterable[Any], *, key: Any) -> list[Any]:
    return sorted(rows, key=key)


def _rejectDuplicateNames(activeClasses: Sequence[OntologyClass]) -> None:
    seen: set[str] = set()
    for row in activeClasses:
        if row.class_name in seen:
            raise ValueError(
                f"本体数据异常：活跃类名 {row.class_name!r} 存在多行（应为每类一行）"
            )
        seen.add(row.class_name)


def _groupActiveProperties(
    properties: Iterable[OntologyProperty],
    allClassById: dict[int, OntologyClass],
) -> dict[int, list[dict[str, Any]]]:
    """按 class_id 分组；墓碑类名下的属性整体丢弃。"""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in properties:
        owner = allClassById.get(row.class_id)
        if owner is None:
            raise ValueError(
                f"本体数据异常：ontology_property 引用了不存在的 class_id={row.class_id}"
            )
        if owner.valid_to is not None:
            continue
        grouped.setdefault(row.class_id, []).append(_compileProperty(row, allClassById))

    return {
        classId: _sortBy(items, key=lambda item: item["propertyName"])
        for classId, items in grouped.items()
    }


def _compileProperty(
    row: OntologyProperty, allClassById: dict[int, OntologyClass]
) -> dict[str, Any]:
    return {
        "propertyName": row.property_name,
        "propertyAlias": row.property_alias,
        "businessAliases": _asList(row.business_aliases),
        "description": row.description,
        "dataType": row.data_type,
        "isPrimaryKey": bool(row.is_primary_key),
        "isForeignKey": bool(row.is_foreign_key),
        "refClassName": _resolveActiveName(row.ref_class_id, allClassById, "ref_class_id"),
        "sourceColumn": row.source_column,
        "unit": row.unit,
        "allowedValues": _asList(row.allowed_values),
        # 三态：None = 未约束；False = 明确允许为空。不可用 bool() 压平。
        "isNotNull": None if row.is_not_null is None else bool(row.is_not_null),
        "minValue": row.min_value,
        "maxValue": row.max_value,
        "regexPattern": row.regex_pattern,
    }


def _compileClass(
    row: OntologyClass,
    allClassById: dict[int, OntologyClass],
    properties: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "className": row.class_name,
        "classAlias": row.class_alias,
        "description": row.description,
        "sourceTable": row.source_table,
        "objectType": row.object_type,
        "objectOwner": row.object_owner,
        "parentClassName": _resolveActiveName(
            row.parent_class_id, allClassById, "parent_class_id"
        ),
        "properties": list(properties),
    }


def _compileJoins(
    joins: Iterable[OntologyJoin],
    allClassById: dict[int, OntologyClass],
    activeNameById: dict[int, str],
) -> list[dict[str, Any]]:
    compiled: list[dict[str, Any]] = []
    for row in joins:
        source = _requireClass(row.source_class_id, allClassById, "source_class_id")
        target = _requireClass(row.target_class_id, allClassById, "target_class_id")
        if source.valid_to is not None or target.valid_to is not None:
            continue
        compiled.append(
            {
                "joinKey": row.join_key,
                "sourceClassName": activeNameById[source.id],
                "sourceColumns": _asList(row.source_columns),
                "targetClassName": activeNameById[target.id],
                "targetColumns": _asList(row.target_columns),
                "joinType": row.join_type,
                "relationType": row.relation_type,
                "description": row.description,
            }
        )
    return _sortBy(compiled, key=lambda item: item["joinKey"])


def _requireClass(
    classId: int | None,
    allClassById: dict[int, OntologyClass],
    fieldName: str,
) -> OntologyClass:
    row = allClassById.get(classId) if classId is not None else None
    if row is None:
        raise ValueError(
            f"本体数据异常：ontology_join {fieldName}={classId} 不存在于 ontology_class"
        )
    return row


def _resolveActiveName(
    classId: int | None,
    allClassById: dict[int, OntologyClass],
    fieldName: str,
) -> str | None:
    """引用活跃类 → 类名；引用墓碑类 → None（目标已不在模型中）。"""
    if classId is None:
        return None
    row = allClassById.get(classId)
    if row is None:
        raise ValueError(f"本体数据异常：{fieldName}={classId} 不存在于 ontology_class")
    return None if row.valid_to is not None else row.class_name


def _asList(value: list[str] | None) -> list[str]:
    """None → []，其余复制一份（不把 DB 对象的列表引用直接暴露给调用方）。"""
    return list(value) if value else []
