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

模块划分（D6 拆分，保持每个文件一屏可读）：
- 本文件：入口（`compileFromRows` / `compileManifest`）+ 本体核心（类 / 属性 / JOIN）；
- `semantic_compiler_sections.py`：指标两层（`metrics` + `kpis`）与业务对象 / 数据域；
- `semantic_manifest_common.py`：类引用解析与稳定排序等共享原语。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    BusinessObject,
    BusinessObjectDomain,
    DataDomain,
    DataSource,
    KpiCatalog,
    OntologyClass,
    OntologyJoin,
    OntologyMetric,
    OntologyMetricSource,
    OntologyProperty,
)
from app.domain.semantic_manifest_schema import SCHEMA_VERSION
from app.services.semantic_compiler_sections import (
    compileBusinessObjects,
    compileDomains,
    compileKpis,
    compileMetrics,
    groupKpiCodesByMetricId,
)
from app.services.semantic_manifest_common import (
    asList,
    requireClass,
    resolveActiveName,
    sortBy,
)


def compileFromRows(
    *,
    classes: Iterable[OntologyClass],
    properties: Iterable[OntologyProperty],
    joins: Iterable[OntologyJoin],
    metrics: Iterable[OntologyMetric] = (),
    metricSources: Iterable[OntologyMetricSource] = (),
    kpis: Iterable[KpiCatalog] = (),
    businessObjects: Iterable[BusinessObject] = (),
    businessObjectDomains: Iterable[BusinessObjectDomain] = (),
    domains: Iterable[DataDomain] = (),
    datasources: Iterable[DataSource] = (),
) -> dict[str, Any]:
    """把治理表行编译成 Manifest 字典（纯函数，无 IO）。

    参数默认 `()` 只是为了让只关心本体的用例免于重复传参；**真实发布必须走
    `compileManifest`** —— 它显式传全 10 张表，漏传会让快照悄悄缺一整个分区。
    """
    indexes = _buildIndexes(classes, properties, metrics, kpis, datasources)
    allClassById = indexes.allClassById
    activeNameById = indexes.activeNameById

    return {
        "schemaVersion": SCHEMA_VERSION,
        "domains": compileDomains(domains),
        "classes": [
            _compileClass(row, allClassById, indexes.propsByClassId.get(row.id, ()))
            for row in indexes.activeClasses
        ],
        "joins": _compileJoins(joins, allClassById, activeNameById),
        "metrics": compileMetrics(
            indexes.allMetrics,
            metricSources,
            allClassById,
            activeNameById,
            groupKpiCodesByMetricId(indexes.allKpis),
        ),
        "kpis": compileKpis(
            indexes.allKpis, indexes.metricNameById, indexes.datasourceNameById
        ),
        "businessObjects": compileBusinessObjects(
            businessObjects,
            businessObjectDomains,
            allClassById,
            activeNameById,
            {row.code for row in domains},
        ),
    }


class _Indexes(NamedTuple):
    """一次编译里反复用到的行索引（避免 `compileFromRows` 主体过长）。"""

    allClassById: dict[int, OntologyClass]
    activeClasses: list[OntologyClass]
    activeNameById: dict[int, str]
    propsByClassId: dict[int, list[dict[str, Any]]]
    allMetrics: list[OntologyMetric]
    allKpis: list[KpiCatalog]
    metricNameById: dict[int, str]
    datasourceNameById: dict[int, str]


def _buildIndexes(
    classes: Iterable[OntologyClass],
    properties: Iterable[OntologyProperty],
    metrics: Iterable[OntologyMetric],
    kpis: Iterable[KpiCatalog],
    datasources: Iterable[DataSource],
) -> _Indexes:
    allClasses = list(classes)
    allClassById = {row.id: row for row in allClasses}
    activeClasses = sortBy(
        (row for row in allClasses if row.valid_to is None),
        key=lambda row: row.class_name,
    )
    _rejectDuplicateNames(activeClasses)
    allMetrics = list(metrics)
    return _Indexes(
        allClassById=allClassById,
        activeClasses=activeClasses,
        activeNameById={row.id: row.class_name for row in activeClasses},
        propsByClassId=_groupActiveProperties(properties, allClassById),
        allMetrics=allMetrics,
        allKpis=list(kpis),
        metricNameById={row.id: row.metric_name for row in allMetrics},
        datasourceNameById={row.id: row.name for row in datasources},
    )


async def compileManifest(session: AsyncSession) -> dict[str, Any]:
    """从真实治理表取数并编译（发布/校验时调用，非高频路径）。"""
    classes = (await session.execute(select(OntologyClass))).scalars().all()
    properties = (await session.execute(select(OntologyProperty))).scalars().all()
    joins = (await session.execute(select(OntologyJoin))).scalars().all()
    metrics = (await session.execute(select(OntologyMetric))).scalars().all()
    metricSources = (
        await session.execute(select(OntologyMetricSource))
    ).scalars().all()
    kpis = (await session.execute(select(KpiCatalog))).scalars().all()
    businessObjects = (await session.execute(select(BusinessObject))).scalars().all()
    businessObjectDomains = (
        await session.execute(select(BusinessObjectDomain))
    ).scalars().all()
    domains = (await session.execute(select(DataDomain))).scalars().all()
    datasources = (await session.execute(select(DataSource))).scalars().all()
    return compileFromRows(
        classes=classes,
        properties=properties,
        joins=joins,
        metrics=metrics,
        metricSources=metricSources,
        kpis=kpis,
        businessObjects=businessObjects,
        businessObjectDomains=businessObjectDomains,
        domains=domains,
        datasources=datasources,
    )


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


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
        classId: sortBy(items, key=lambda item: item["propertyName"])
        for classId, items in grouped.items()
    }


def _compileProperty(
    row: OntologyProperty, allClassById: dict[int, OntologyClass]
) -> dict[str, Any]:
    return {
        "propertyName": row.property_name,
        "propertyAlias": row.property_alias,
        "businessAliases": asList(row.business_aliases),
        "description": row.description,
        "dataType": row.data_type,
        "isPrimaryKey": bool(row.is_primary_key),
        "isForeignKey": bool(row.is_foreign_key),
        "refClassName": resolveActiveName(row.ref_class_id, allClassById, "ref_class_id"),
        "sourceColumn": row.source_column,
        "unit": row.unit,
        "allowedValues": asList(row.allowed_values),
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
        "parentClassName": resolveActiveName(
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
        source = requireClass(row.source_class_id, allClassById, "source_class_id")
        target = requireClass(row.target_class_id, allClassById, "target_class_id")
        if source.valid_to is not None or target.valid_to is not None:
            continue
        compiled.append(
            {
                "joinKey": row.join_key,
                "sourceClassName": activeNameById[source.id],
                "sourceColumns": asList(row.source_columns),
                "targetClassName": activeNameById[target.id],
                "targetColumns": asList(row.target_columns),
                "joinType": row.join_type,
                "relationType": row.relation_type,
                "description": row.description,
            }
        )
    return sortBy(compiled, key=lambda item: item["joinKey"])
