"""语义发布的图谱镜像：把发布快照（manifest）物化成不可变的图谱镜像。

**一句话**：发布版本 = 一份**只读**的图谱快照；本模块负责把 manifest 翻译成
「镜像节点 + 镜像边 + 未解析清单」，写图由 `app/infrastructure/neo4j_mirror_client.py`
的镜像写函数完成。

三条设计约束（依据 `Harness/changes/feat-semantic-release-graph-mirror/daily/D22.md`）：

1. **主图零影响**：镜像节点用独立 label（`ReleaseClass` 等）⇒ 主图消费方
   `MATCH (c:Class)` 不命中镜像。
2. **幂等**：节点身份 = (label, unified_id, release_id)；边身份 = (relType, 两端节点)。
   同一发布重复物化结果完全一致。
3. **解析失败必须可见**：manifest 用**名字**标识实体，图谱用 `unified_id`；
   解析不到 ⇒ 该实体不进镜像，且**逐条**记进未解析清单（不静默跳过）。

本模块是**纯逻辑**（不碰 IO）⇒ 全部可单测；碰 PG 的解析在
`app/services/semantic_graph_mirror_resolution.py`。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from app.domain.semantic_graph_mirror_spec import (
    RELEASE_MIRROR_ENTITY_TYPES,
    RELEASE_MIRROR_NODE_FIELDS,
)

_TYPE_ORDER = {name: index for index, name in enumerate(RELEASE_MIRROR_ENTITY_TYPES)}


class ManifestShapeError(ValueError):
    """manifest 结构不符合契约（缺必填字段等）—— 失败要**早**且说得清。"""


@dataclass(frozen=True)
class EntityResolution:
    """名字 → unified_id 的解析结果。

    值语义（三种「解析不到」要分开说，否则报告没法指导排查）：

    | 状态 | 表现 | 含义 |
    |---|---|---|
    | 已解析 | 索引里有键，值是字符串 | 拿到 `unified_id` |
    | 歧义 | 索引里有键，值是 `None` | **同名多行**，无法确定是哪个实体 |
    | 未登记 | 键不在索引里，但在 `unmapped` 里 | 实体存在，但 `id_mapping` 没登记 |
    | 不存在 | 键不在索引里，也不在 `unmapped` 里 | 本体里根本没这个实体 |
    """

    classIds: Mapping[str, str | None] = field(default_factory=dict)
    propertyIds: Mapping[tuple[str, str], str | None] = field(default_factory=dict)
    metricIds: Mapping[str, str | None] = field(default_factory=dict)
    kpiIds: Mapping[str, str | None] = field(default_factory=dict)
    # 实体类型 → 「存在但没登记统一 ID」的键集合
    unmapped: Mapping[str, frozenset[Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class MirrorNode:
    """一个镜像节点（还未绑定发布标识 ⇒ 与发布无关的纯形态）。"""

    entityType: str
    unifiedId: str
    fields: tuple[tuple[str, Any], ...]

    def toRow(self, *, releaseId: int, releaseVersion: str) -> dict[str, Any]:
        """转成写图用的行：unified_id + 发布标识 + 实体字段。"""
        row: dict[str, Any] = {
            "unified_id": self.unifiedId,
            "release_id": releaseId,
            "release_version": releaseVersion,
            # mirror_of = 主图实体的 unified_id（与自身同值，显式冗余便于回查）
            "mirror_of": self.unifiedId,
        }
        row.update(dict(self.fields))
        return row


@dataclass(frozen=True)
class MirrorEdge:
    """一条镜像边：发布作用域由**端点**承载，边本身不带属性、不带键。"""

    relType: str
    sourceType: str
    sourceUid: str
    targetType: str
    targetUid: str


@dataclass(frozen=True)
class UnresolvedItem:
    """一条未解析记录（实体或边），逐条报告，绝不静默跳过。

    - `entityType`：实体类型（class/property/metric/kpi）；边记录里是**源**实体类型；
    - `relType`：仅边记录有值（关系类型），实体记录为空串。
    """

    kind: str  # "entity" | "edge"
    entityType: str
    name: str
    reason: str
    relType: str = ""


@dataclass(frozen=True)
class MirrorPlan:
    """一次物化的完整计划（纯数据，可 JSON 化、可复算）。"""

    releaseId: int
    releaseVersion: str
    nodes: tuple[MirrorNode, ...]
    edges: tuple[MirrorEdge, ...]
    unresolved: tuple[UnresolvedItem, ...]

    def nodeRowsByType(self) -> dict[str, list[dict[str, Any]]]:
        """按实体类型分组的写图行（分批写入按类型走 ⇒ 事务小、易续跑）。"""
        grouped: dict[str, list[dict[str, Any]]] = {
            name: [] for name in RELEASE_MIRROR_ENTITY_TYPES
        }
        for node in self.nodes:
            grouped[node.entityType].append(
                node.toRow(releaseId=self.releaseId, releaseVersion=self.releaseVersion)
            )
        return grouped

    def edgeRows(self) -> list[dict[str, Any]]:
        """写图用的边行。"""
        return [
            {
                "relType": edge.relType,
                "sourceType": edge.sourceType,
                "sourceUid": edge.sourceUid,
                "targetType": edge.targetType,
                "targetUid": edge.targetUid,
            }
            for edge in self.edges
        ]


# =============================================================================
# 名字 → unified_id 的解析表（纯函数）
# =============================================================================


def buildEntityResolution(
    *,
    classRows: Iterable[tuple[str, str | None]],
    propertyRows: Iterable[tuple[str, str, str | None]],
    metricRows: Iterable[tuple[str, str | None]],
    kpiRows: Iterable[tuple[str, str | None]],
) -> EntityResolution:
    """把 PG 行折成「名字 → unified_id」解析表。

    行里的 `unified_id` 允许为 `None`（实体存在但 `id_mapping` 没登记）⇒ 该键进
    `unmapped` 集合，不进解析索引。

    同名**同** unified_id 的重复行不算歧义（同一实体被 join 放大）；
    同名**不同** unified_id ⇒ 歧义（值记 `None`）⇒ 计划里记为未解析，
    因为无法确定该镜像哪一个实体。
    """
    classIds, unmappedClasses = _indexByKey(classRows, lambda row: row[0], uidIndex=1)
    propertyIds, unmappedProperties = _indexByKey(
        propertyRows, lambda row: (row[0], row[1]), uidIndex=2
    )
    metricIds, unmappedMetrics = _indexByKey(metricRows, lambda row: row[0], uidIndex=1)
    kpiIds, unmappedKpis = _indexByKey(kpiRows, lambda row: row[0], uidIndex=1)
    return EntityResolution(
        classIds=MappingProxyType(classIds),
        propertyIds=MappingProxyType(propertyIds),
        metricIds=MappingProxyType(metricIds),
        kpiIds=MappingProxyType(kpiIds),
        unmapped=MappingProxyType(
            {
                "class": unmappedClasses,
                "property": unmappedProperties,
                "metric": unmappedMetrics,
                "kpi": unmappedKpis,
            }
        ),
    )


def _indexByKey(
    rows: Iterable[tuple[Any, ...]],
    keyOf: Callable[[tuple[Any, ...]], Any],
    *,
    uidIndex: int,
) -> tuple[dict[Any, str | None], frozenset[Any]]:
    """按 key 索引 unified_id；返回 (解析索引, 未登记集合)。

    - 同 key 只有一个非空 uid ⇒ 解析成功；
    - 同 key 有多个**不同**非空 uid ⇒ 歧义（值 `None`）；
    - 同 key 的 uid 全为空 ⇒ 实体存在但未登记统一 ID（进未登记集合）。

    `uidIndex` 显式给出 uid 所在列（不靠「最后一列是 unified_id」的隐式约定）。
    """
    uidsByKey: dict[Any, set[str]] = {}
    unmapped: set[Any] = set()
    for row in rows:
        key = keyOf(row)
        unifiedId = row[uidIndex]
        if unifiedId is None:
            unmapped.add(key)
            continue
        uidsByKey.setdefault(key, set()).add(unifiedId)
    index: dict[Any, str | None] = {}
    for key, uids in uidsByKey.items():
        index[key] = next(iter(uids)) if len(uids) == 1 else None
    return index, frozenset(unmapped)


# =============================================================================
# 镜像计划（纯函数）
# =============================================================================


@dataclass
class _PlanContext:
    """计划构建过程中的可变累加器（局部对象 ⇒ 不外泄、不共享）。"""

    nodes: dict[tuple[str, str], MirrorNode] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], MirrorEdge] = field(default_factory=dict)
    unresolved: list[UnresolvedItem] = field(default_factory=list)
    classUidByName: dict[str, str] = field(default_factory=dict)

    def addNode(
        self, entityType: str, unifiedId: str, values: Mapping[str, Any]
    ) -> None:
        """登记一个镜像节点（同一 (类型, unified_id) 只登记一次）。"""
        key = (entityType, unifiedId)
        if key in self.nodes:
            return
        fields = tuple(
            (name, values.get(name)) for name in RELEASE_MIRROR_NODE_FIELDS[entityType]
        )
        self.nodes[key] = MirrorNode(
            entityType=entityType, unifiedId=unifiedId, fields=fields
        )

    def addEdge(
        self,
        relType: str,
        sourceType: str,
        sourceUid: str,
        targetType: str,
        targetUid: str,
    ) -> None:
        """登记一条镜像边（同一 (relType, 源, 目标) 只登记一次）。"""
        key = (relType, sourceUid, targetUid)
        if key not in self.edges:
            self.edges[key] = MirrorEdge(
                relType=relType,
                sourceType=sourceType,
                sourceUid=sourceUid,
                targetType=targetType,
                targetUid=targetUid,
            )

    def addUnresolved(
        self,
        kind: str,
        entityType: str,
        name: str,
        reason: str,
        relType: str = "",
    ) -> None:
        self.unresolved.append(
            UnresolvedItem(
                kind=kind,
                entityType=entityType,
                name=name,
                reason=reason,
                relType=relType,
            )
        )


def buildMirrorPlan(
    manifest: Mapping[str, Any],
    resolution: EntityResolution,
    *,
    releaseId: int,
    releaseVersion: str,
) -> MirrorPlan:
    """manifest + 名字解析结果 ⇒ 镜像节点 / 镜像边 / 未解析清单。

    规则：
    - 类的属性若**类未解析** ⇒ 属性级联记为未解析（无父节点的属性节点是孤儿）；
    - 边的任一端点未解析 ⇒ 该边不产出，并记一条 `kind="edge"` 的未解析记录；
    - 节点/边去重且**排序稳定** ⇒ 同一份输入永远得到同一份计划（可复算、可对账）。
    """
    ctx = _PlanContext()
    _planClasses(manifest, resolution, ctx)
    _planSubclassEdges(manifest, resolution, ctx)
    _planJoinEdges(manifest, resolution, ctx)
    _planMetrics(manifest, resolution, ctx)
    _planKpis(manifest, resolution, ctx)
    return MirrorPlan(
        releaseId=releaseId,
        releaseVersion=releaseVersion,
        nodes=tuple(
            sorted(
                ctx.nodes.values(),
                key=lambda node: (_TYPE_ORDER[node.entityType], node.unifiedId),
            )
        ),
        edges=tuple(
            sorted(
                ctx.edges.values(),
                key=lambda edge: (edge.relType, edge.sourceUid, edge.targetUid),
            )
        ),
        unresolved=tuple(
            sorted(
                ctx.unresolved,
                key=lambda item: (
                    item.kind,
                    item.entityType,
                    item.relType,
                    item.name,
                    item.reason,
                ),
            )
        ),
    )


def _planClasses(
    manifest: Mapping[str, Any],
    resolution: EntityResolution,
    ctx: _PlanContext,
) -> None:
    """类 + 属性（含 HAS_PROPERTY / REFERENCES 边）。"""
    for cls in manifest.get("classes") or []:
        className = _requireName(cls, "className", "classes[]")
        unifiedId = _resolve(resolution.classIds, className)
        if unifiedId is None:
            _recordUnresolvedClass(cls, className, resolution, ctx)
            continue
        ctx.classUidByName[className] = unifiedId
        ctx.addNode(
            "class",
            unifiedId,
            {
                "name": className,
                "alias": cls.get("classAlias"),
                "description": cls.get("description"),
                "sourceTable": cls.get("sourceTable"),
            },
        )
        for prop in cls.get("properties") or []:
            _planProperty(className, prop, unifiedId, resolution, ctx)


def _recordUnresolvedClass(
    cls: Mapping[str, Any],
    className: str,
    resolution: EntityResolution,
    ctx: _PlanContext,
) -> None:
    """类未解析 ⇒ 记一条；其属性**级联**记为未解析（孤儿属性不入镜像）。"""
    ctx.addUnresolved(
        "entity", "class", className, _reason(resolution, "class", className)
    )
    for prop in cls.get("properties") or []:
        propName = prop.get("propertyName")
        ctx.addUnresolved(
            "entity",
            "property",
            f"{className}.{propName}",
            "所属类未解析",
        )


def _planProperty(
    className: str,
    prop: Mapping[str, Any],
    classUid: str,
    resolution: EntityResolution,
    ctx: _PlanContext,
) -> None:
    """属性节点 + HAS_PROPERTY 边 + REFERENCES 边（属性 → 被引用类）。"""
    propName = _requireName(prop, "propertyName", "properties[]")
    propUid = _resolve(resolution.propertyIds, (className, propName))
    if propUid is None:
        ctx.addUnresolved(
            "entity",
            "property",
            f"{className}.{propName}",
            _reason(resolution, "property", (className, propName)),
        )
        return
    ctx.addNode(
        "property",
        propUid,
        {
            "name": propName,
            "alias": prop.get("propertyAlias"),
            "dataType": prop.get("dataType"),
            "sourceColumn": prop.get("sourceColumn"),
            "isPrimaryKey": bool(prop.get("isPrimaryKey")),
            "isForeignKey": bool(prop.get("isForeignKey")),
        },
    )
    ctx.addEdge("HAS_PROPERTY", "class", classUid, "property", propUid)
    _planReferenceEdge(className, propName, propUid, prop, resolution, ctx)


def _planReferenceEdge(
    className: str,
    propName: str,
    propUid: str,
    prop: Mapping[str, Any],
    resolution: EntityResolution,
    ctx: _PlanContext,
) -> None:
    """REFERENCES 边（属性 → 被引用类）：manifest 自带 `refClassName`。

    悬空引用（目标类已进墓碑）在**编译器**里就编成了 null ⇒ 这里不会有边，
    也不会有「未解析」噪音。
    """
    refClassName = prop.get("refClassName")
    if not refClassName:
        return
    refUid = _resolve(resolution.classIds, refClassName)
    if refUid is None:
        ctx.addUnresolved(
            "edge",
            "property",
            f"{className}.{propName}->{refClassName}",
            "端点未解析",
            relType="REFERENCES",
        )
        return
    ctx.addEdge("REFERENCES", "property", propUid, "class", refUid)


def _planSubclassEdges(
    manifest: Mapping[str, Any],
    resolution: EntityResolution,
    ctx: _PlanContext,
) -> None:
    """父子类边（SUBCLASS_OF）。"""
    for cls in manifest.get("classes") or []:
        className = cls.get("className")
        parentName = cls.get("parentClassName")
        if not parentName:
            continue
        sourceUid = ctx.classUidByName.get(className)
        targetUid = _resolve(resolution.classIds, parentName)
        if sourceUid is None or targetUid is None:
            ctx.addUnresolved(
                "edge",
                "class",
                f"{className}->{parentName}",
                "端点未解析",
                relType="SUBCLASS_OF",
            )
            continue
        ctx.addEdge("SUBCLASS_OF", "class", sourceUid, "class", targetUid)


def _planJoinEdges(
    manifest: Mapping[str, Any],
    resolution: EntityResolution,
    ctx: _PlanContext,
) -> None:
    """JOIN 边（源类 → 目标类）。

    图边**不带属性** ⇒ 同一对类之间的多条 join（不同 joinKey）在图上是同一条
    ⇒ 按 (relType, 源, 目标) 去重（与主图同粒度）。
    """
    for join in manifest.get("joins") or []:
        sourceName = _requireName(join, "sourceClassName", "joins[]")
        targetName = _requireName(join, "targetClassName", "joins[]")
        sourceUid = _resolve(resolution.classIds, sourceName)
        targetUid = _resolve(resolution.classIds, targetName)
        if sourceUid is None or targetUid is None:
            ctx.addUnresolved(
                "edge",
                "class",
                f"{sourceName}->{targetName}",
                "端点未解析",
                relType="JOIN",
            )
            continue
        ctx.addEdge("JOIN", "class", sourceUid, "class", targetUid)


def _planMetrics(
    manifest: Mapping[str, Any],
    resolution: EntityResolution,
    ctx: _PlanContext,
) -> None:
    """指标节点 + DERIVED_FROM 边（指标 → 目标类）。"""
    for metric in manifest.get("metrics") or []:
        metricName = _requireName(metric, "metricName", "metrics[]")
        unifiedId = _resolve(resolution.metricIds, metricName)
        if unifiedId is None:
            ctx.addUnresolved(
                "entity",
                "metric",
                metricName,
                _reason(resolution, "metric", metricName),
            )
            continue
        ctx.addNode(
            "metric",
            unifiedId,
            {
                "name": metricName,
                "alias": metric.get("metricAlias"),
                "formula": metric.get("formula"),
                "aggFunction": metric.get("aggFunction"),
            },
        )
        targetName = metric.get("targetClassName")
        if not targetName:
            continue
        targetUid = _resolve(resolution.classIds, targetName)
        if targetUid is None:
            ctx.addUnresolved(
                "edge",
                "metric",
                f"{metricName}->{targetName}",
                "端点未解析",
                relType="DERIVED_FROM",
            )
            continue
        ctx.addEdge("DERIVED_FROM", "metric", unifiedId, "class", targetUid)


def _planKpis(
    manifest: Mapping[str, Any],
    resolution: EntityResolution,
    ctx: _PlanContext,
) -> None:
    """KPI 节点 + MEASURED_BY 边（KPI → 指标）。"""
    for kpi in manifest.get("kpis") or []:
        kpiCode = _requireName(kpi, "kpiCode", "kpis[]")
        unifiedId = _resolve(resolution.kpiIds, kpiCode)
        if unifiedId is None:
            ctx.addUnresolved(
                "entity", "kpi", kpiCode, _reason(resolution, "kpi", kpiCode)
            )
            continue
        ctx.addNode(
            "kpi",
            unifiedId,
            {
                "code": kpiCode,
                "name": kpi.get("kpiName"),
                "status": kpi.get("status"),
                "owner": kpi.get("owner"),
                "unit": kpi.get("unit"),
            },
        )
        metricName = kpi.get("metricName")
        if not metricName:
            continue
        metricUid = _resolve(resolution.metricIds, metricName)
        if metricUid is None:
            ctx.addUnresolved(
                "edge",
                "kpi",
                f"{kpiCode}->{metricName}",
                "端点未解析",
                relType="MEASURED_BY",
            )
            continue
        ctx.addEdge("MEASURED_BY", "kpi", unifiedId, "metric", metricUid)


def _requireName(item: Mapping[str, Any], key: str, where: str) -> str:
    """取 manifest 里的必填名字字段；缺了要**早失败**且说得清。"""
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestShapeError(f"manifest {where} 缺 {key}: {item!r}")
    return value


def _resolve(index: Mapping[Any, str | None], key: Any) -> str | None:
    """解析名字：区分「歧义（值为 None）」与「不存在（无键）」。"""
    if key not in index:
        return None
    return index[key]


def _reason(resolution: EntityResolution, entityType: str, key: Any) -> str:
    """未解析原因文案（三种成因分开说，报告才能指导排查）。"""
    index = _indexOf(resolution, entityType)
    if key in index and index[key] is None:
        return "同名多行，实体歧义"
    if key in resolution.unmapped.get(entityType, frozenset()):
        return "该实体未登记统一 ID（id_mapping 缺行）"
    return "本体中不存在该实体"


def _indexOf(resolution: EntityResolution, entityType: str) -> Mapping[Any, str | None]:
    """取某个实体类型的解析索引。"""
    return {
        "class": resolution.classIds,
        "property": resolution.propertyIds,
        "metric": resolution.metricIds,
        "kpi": resolution.kpiIds,
    }[entityType]


__all__ = [
    "EntityResolution",
    "ManifestShapeError",
    "MirrorEdge",
    "MirrorNode",
    "MirrorPlan",
    "UnresolvedItem",
    "buildEntityResolution",
    "buildMirrorPlan",
]
