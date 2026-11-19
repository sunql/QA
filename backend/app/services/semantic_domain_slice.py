"""发布域切片（M1-1.3 批次 B / D-10）：全域快照 → 「某个域能发布的那一份」。

为什么需要这一层
----------------
发布快照可以是**全域**的（一次编译把治理表全量编进 manifest），但采购域的发布
必须只带采购域的东西：把质量域的类一起发出去，等于「换个 domain 参数结果不变」。

切片口径（manifest 里**没有** class↔domain 的直连字段，只有业务对象挂域）
------------------------------------------------------------------------
1. **业务对象**：`primaryDomainCode == 域` 或 `attachedDomainCodes` 含该域。
2. **类**：业务对象的 `headerClassName` + 它们的**父类闭包**（结构性依赖）。
3. **JOIN**：两端都在类集合里。
4. **指标**：`targetClassName` / `numeratorMeasure.className` /
   `denominatorMeasure.className` / `sources[].className` 命中类集合。
5. **KPI**：`metricName` 命中本域指标，或 `kpiCode` 出现在本域指标的 `kpiCodes`。

⚠️ 归不了属的条目（无域锚点）**一律排除**（fail-closed）：宁可少给，
也不把别的域的东西端上来。未知域 ⇒ **空切片**（不是「全量」，也不是报错）。

发布切片比检索切片多两件事（**引用闭合**，D-10 §2.4）
----------------------------------------------------
切片里的指标若引用了切片外的类（分子 / 分母 / 数据源类），v2 的引用契约会判
「不存在」⇒ 快照**发不出去**。⇒ 两种处置：剔除越界指标（静默丢 KPI）或
**引用闭合补齐**（把被引用类及其父类闭包补进切片）。**采纳后者**：
契约成立 + 事实不丢；跨域依赖以 `DOMAIN_SLICE_CROSS_DOMAIN` 显式暴露。

闭合有**四条来源**（缺一条 ⇒ 域发布必然过不了契约）：
1. **度量引用**：指标的分子 / 分母 / 数据源类；
2. **属性引用**：类的 `properties[].refClassName`（字典型引用，常见建模）；
3. **JOIN 邻居**：孤岛判据（`SOURCE_BINDING_JOIN_MISSING`）问的是「这个类在
   整个本体里有没有 JOIN 边」——如果只看切片，**只连跨域边**的类会被误判成
   孤岛 ⇒ 误阻断合法发布。⇒ 对**有绑定的类**，把它的一跳 JOIN 邻居也拉进
   切片（邻居的父类闭包同理）：这样「切片里有没有边」与「本体里有没有边」
   **完全等价**，判据既不漏也不误；
4. **父类闭包**：补进来的类的父类。

四条来源互相喂新类 ⇒ 取**联合不动点**（迭代到稳定）⇒ 切片**幂等**
（对切片再切一次得同一份）⇒ 发布校验项可从**已存快照**复算。

本模块是**纯函数**（不吃 DB、不吃 session）⇒ 可直接单测。
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.semantic_manifest_schema import (
    MANIFEST_SCHEMA_SOURCE_BINDING_VERSIONS,
    SCHEMA_VERSION,
)


@dataclass(frozen=True, slots=True)
class DomainSlice:
    """某个域在发布快照里对应的条目集合。

    `domains` / `sourceBindings` / `closureClassNames` 是**发布切片**才有的字段
    （检索切片不用它们 ⇒ 默认空元组，保持既有构造方式可用）。
    """

    domainCode: str
    classNames: tuple[str, ...]
    classes: tuple[dict[str, Any], ...]
    joins: tuple[dict[str, Any], ...]
    businessObjects: tuple[dict[str, Any], ...]
    metrics: tuple[dict[str, Any], ...]
    kpis: tuple[dict[str, Any], ...]
    domains: tuple[dict[str, Any], ...] = ()
    sourceBindings: tuple[dict[str, Any], ...] = ()
    closureClassNames: tuple[str, ...] = ()
    """引用闭合补齐引入的类（切片原本没有）⇒ 跨域依赖的显式暴露。"""


def _partition(manifest: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """取一个分区（缺分区 / 非列表 ⇒ 空列表）。"""
    value = manifest.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _businessObjectsOfDomain(
    businessObjects: Iterable[dict[str, Any]], domainCode: str
) -> list[dict[str, Any]]:
    """主域或附域命中该域的业务对象。"""
    hits: list[dict[str, Any]] = []
    for bo in businessObjects:
        if bo.get("primaryDomainCode") == domainCode:
            hits.append(bo)
            continue
        attached = bo.get("attachedDomainCodes")
        if isinstance(attached, list) and domainCode in attached:
            hits.append(bo)
    return hits


def _classClosure(
    classes: Iterable[dict[str, Any]], seedNames: Iterable[str]
) -> set[str]:
    """种子类 + 它们的**父类闭包**（结构性依赖，跨域也带上）。"""
    byName = {
        cls.get("className"): cls
        for cls in classes
        if isinstance(cls.get("className"), str)
    }
    closure: set[str] = set()
    pending = [name for name in seedNames if isinstance(name, str)]
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        parent = byName.get(name, {}).get("parentClassName")
        if isinstance(parent, str) and parent not in closure:
            pending.append(parent)
    return closure


def _metricClassNames(metric: Mapping[str, Any]) -> set[str]:
    """指标锚定的类名（目标类 + 分子/分母度量 + 数据源类）。"""
    names: set[str] = set()
    target = metric.get("targetClassName")
    if isinstance(target, str):
        names.add(target)
    for key in ("numeratorMeasure", "denominatorMeasure"):
        measure = metric.get(key)
        if isinstance(measure, Mapping):
            name = measure.get("className")
            if isinstance(name, str):
                names.add(name)
    sources = metric.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, Mapping):
                name = source.get("className")
                if isinstance(name, str):
                    names.add(name)
    return names


def _propertyRefClassNames(
    classes: Iterable[Mapping[str, Any]], classNames: set[str]
) -> set[str]:
    """选中类的**属性引用**到的类名（`properties[].refClassName`）。

    为什么必须闭：v2 引用契约要求「属性的 `refClassName` 必须存在于切片里」
    （`semantic_manifest_schema._classSectionErrors`）⇒ 不闭 ⇒ 域快照
    **过不了契约** ⇒ 该域永远发不出去（而且是裸 `ValueError` ⇒ 500）。

    ⚠️ 这是「表引用型字典」的常见建模（属性指向另一张表的类）⇒ 不是假想场景。
    """
    refs: set[str] = set()
    for cls in classes:
        name = cls.get("className")
        if not isinstance(name, str) or name not in classNames:
            continue
        properties = cls.get("properties")
        if not isinstance(properties, list):
            continue
        for prop in properties:
            if not isinstance(prop, Mapping):
                continue
            ref = prop.get("refClassName")
            if isinstance(ref, str):
                refs.add(ref)
    return refs


def sliceManifestByDomain(
    manifest: Mapping[str, Any],
    domainCode: str,
    *,
    entityTypes: Sequence[str] | None = None,
) -> DomainSlice:
    """把发布 manifest 切成某个域的**检索**切片。

    Args:
        manifest: 发布快照正文（`SemanticReleaseSnapshot.manifest`）。
        domainCode: 目标域编码。
        entityTypes: **请求侧收窄**的业务对象码（`None` = 不收窄）。
            传空元组 ⇒ 收窄到空 ⇒ 空切片（调用方要的是「只要它」，
            而它不可见 ⇒ 什么都给不了，**不许**回落成「全都给」）。

    Returns:
        域切片；域不存在或无锚点 ⇒ 空切片。
    """
    allClasses = _partition(manifest, "classes")
    allJoins = _partition(manifest, "joins")
    allBusinessObjects = _partition(manifest, "businessObjects")
    allMetrics = _partition(manifest, "metrics")
    allKpis = _partition(manifest, "kpis")

    businessObjects = _businessObjectsOfDomain(allBusinessObjects, domainCode)
    if entityTypes is not None:
        allowed = set(entityTypes)
        businessObjects = [
            bo for bo in businessObjects if bo.get("code") in allowed
        ]
    seedNames = {
        bo.get("headerClassName")
        for bo in businessObjects
        if isinstance(bo.get("headerClassName"), str)
    }
    classNames = _classClosure(allClasses, seedNames)

    classes = tuple(
        sorted(
            (cls for cls in allClasses if cls.get("className") in classNames),
            key=lambda cls: str(cls.get("className")),
        )
    )
    joins = tuple(
        sorted(
            (
                join
                for join in allJoins
                if join.get("sourceClassName") in classNames
                and join.get("targetClassName") in classNames
            ),
            key=lambda join: str(join.get("joinKey")),
        )
    )
    metrics = tuple(
        sorted(
            (
                metric
                for metric in allMetrics
                if _metricClassNames(metric) & classNames
            ),
            key=lambda metric: str(metric.get("metricName")),
        )
    )

    metricNames = {
        metric.get("metricName") for metric in metrics if metric.get("metricName")
    }
    metricKpiCodes: set[str] = set()
    for metric in metrics:
        codes = metric.get("kpiCodes")
        if isinstance(codes, list):
            metricKpiCodes.update(code for code in codes if isinstance(code, str))
    kpis = tuple(
        sorted(
            (
                kpi
                for kpi in allKpis
                if kpi.get("metricName") in metricNames
                or kpi.get("kpiCode") in metricKpiCodes
            ),
            key=lambda kpi: str(kpi.get("kpiCode")),
        )
    )

    return DomainSlice(
        domainCode=domainCode,
        classNames=tuple(sorted(classNames)),
        classes=classes,
        joins=joins,
        businessObjects=tuple(
            sorted(businessObjects, key=lambda bo: str(bo.get("code")))
        ),
        metrics=metrics,
        kpis=kpis,
    )


def sliceManifestForRelease(
    manifest: Mapping[str, Any], domainCode: str
) -> DomainSlice:
    """切成某个域的**发布**切片（引用闭合补齐 + 绑定分区 + 域词表）。

    与检索切片的三点差别：

    1. **引用闭合补齐**：本域指标引用到的类、属性的 `refClassName`、
       有绑定类的 JOIN 邻居，以及它们的父类闭包，全部补进切片 ⇒ 快照满足
       引用契约。补进来的类记在 `closureClassNames`（跨域依赖的显式暴露）。
    2. **绑定分区**：`sourceBindings` 按「所属类在切片里」筛选后入档。
    3. **域词表**：`domains` 取「本域 + 切片内业务对象引用的域」，
       否则业务对象的 `primaryDomainCode` 会被引用闸判成「域名不存在」。
    """
    base = sliceManifestByDomain(manifest, domainCode)
    allClasses = _partition(manifest, "classes")
    allJoins = _partition(manifest, "joins")
    allBindings = _partition(manifest, "sourceBindings")

    selected = _closure(base, allClasses, allJoins, allBindings)
    closureClassNames = tuple(sorted(selected - set(base.classNames)))
    classes = _copyPartitions(
        sorted(
            (cls for cls in allClasses if cls.get("className") in selected),
            key=lambda cls: str(cls.get("className")),
        )
    )
    joins = _copyPartitions(
        sorted(
            (
                join
                for join in allJoins
                if join.get("sourceClassName") in selected
                and join.get("targetClassName") in selected
            ),
            key=lambda join: str(join.get("joinKey")),
        )
    )
    sourceBindings = _copyPartitions(
        sorted(
            (
                binding
                for binding in allBindings
                if isinstance(binding.get("className"), str)
                and binding.get("className") in selected
            ),
            key=_bindingSortKey,
        )
    )
    domains = _copyPartitions(
        sorted(
            _domainEntries(manifest, domainCode, base.businessObjects),
            key=lambda entry: str(entry.get("code")),
        )
    )
    return DomainSlice(
        domainCode=domainCode,
        classNames=tuple(sorted(selected)),
        classes=classes,
        joins=joins,
        businessObjects=base.businessObjects,
        metrics=base.metrics,
        kpis=base.kpis,
        domains=domains,
        sourceBindings=sourceBindings,
        closureClassNames=closureClassNames,
    )


def _closure(
    base: DomainSlice,
    allClasses: Sequence[Mapping[str, Any]],
    allJoins: Sequence[Mapping[str, Any]],
    allBindings: Sequence[Mapping[str, Any]],
) -> set[str]:
    """引用闭合的**联合不动点**：切片里最终有哪些类。

    四条来源互相喂新类（新类又有自己的属性引用、自己的绑定、自己的父类）
    ⇒ 必须迭代到稳定，否则会漏：

    1. **度量引用**：本域指标引用到的类（否则快照过不了引用契约）；
    2. **属性引用**：选中类的 `properties[].refClassName`（**字典型引用**的
       常见建模；漏它 ⇒ 域发布必然过不了契约）；
    3. **JOIN 邻居**：只对**有绑定的类**做（孤岛判据只看绑定类），且取不动点 ——
       补进来的邻居若自己也有绑定，它的邻居同样要补，否则快照里会出现
       「类在、边不在」的自相矛盾的类；
    4. **父类闭包**：补进来的类的父类（**必须与 3 交替迭代** ⇒ 父类若自己
       也有绑定，它的 JOIN 邻居也要进来，否则该父类会被误判成孤岛）。

    之所以能收敛：每一步都只增不减，且类名集合有限 ⇒ 必然终止。
    之所以幂等：不动点集合只依赖「输入 manifest + 域切片」，两次计算同参
    ⇒ 同结果 ⇒ 发布校验项可以从**已存快照**复算。
    """
    selected = set(base.classNames)
    while True:
        before = frozenset(selected)
        for metric in base.metrics:
            selected |= _metricClassNames(metric)
        selected |= _propertyRefClassNames(allClasses, selected)
        selected = _joinClosure(allJoins, allBindings, selected)
        selected = _classClosure(allClasses, selected)
        if frozenset(selected) == before:
            return selected


def sliceManifestToDomain(
    manifest: Mapping[str, Any], domainCode: str
) -> dict[str, Any]:
    """按域切片并还原成一份**完整 manifest 字典**（分区齐全 ⇒ 能过契约校验）。

    ⚠️ 版本决定绑定分区在不在（v1 没有 `sourceBindings`）⇒ 必须跟着 manifest
    自己的 `schemaVersion` 走，否则切片出来的形状与编译器不一致 ⇒ 同一份治理
    内容在两条路径上算出不同哈希（字节确定性铁律）。
    """
    version = manifest.get("schemaVersion") or SCHEMA_VERSION
    includeBindings = version in MANIFEST_SCHEMA_SOURCE_BINDING_VERSIONS
    slice_ = sliceManifestForRelease(manifest, domainCode)
    return sliceToManifest(slice_, version, includeSourceBindings=includeBindings)


def sliceToManifest(
    slice_: DomainSlice, schemaVersion: str, *, includeSourceBindings: bool = True
) -> dict[str, Any]:
    """把切片还原成一份**完整的 manifest 字典**（分区齐全 ⇒ 能过契约校验）。

    ⚠️ 分区顺序与字段集合必须与编译器产出的形状一致 ⇒ 否则同一份治理内容
    在「全局发布」与「域发布」两条路径上会算出不同的哈希（字节确定性铁律）。
    """
    manifest: dict[str, Any] = {
        "schemaVersion": schemaVersion,
        "domains": list(slice_.domains),
        "classes": list(slice_.classes),
        "joins": list(slice_.joins),
        "metrics": list(slice_.metrics),
        "kpis": list(slice_.kpis),
        "businessObjects": list(slice_.businessObjects),
    }
    if includeSourceBindings:
        manifest["sourceBindings"] = list(slice_.sourceBindings)
    return manifest


def _joinClosure(
    joins: Iterable[dict[str, Any]],
    bindings: Iterable[dict[str, Any]],
    selected: set[str],
) -> set[str]:
    """把「有绑定类的一跳 JOIN 邻居」补进切片，**取不动点**。

    为什么取不动点：补进来的邻居若自己也有绑定，它的邻居同样要补 ——
    否则快照里会出现「类在、边不在」的类（自相矛盾的图），
    且孤岛判据在「切片 vs 本体」之间不再等价。

    为什么幂等（可复算）：不动点的每一步都只依赖「边 + 绑定」，
    而不动点集合内部的边**全部**保留在切片里 ⇒ 对切片再跑一次得到同一集合
    ⇒ 发布校验项可以只依赖已存快照复算。
    """
    joinList = list(joins)
    bindingList = list(bindings)
    current = set(selected)
    while True:
        boundNames = {
            str(binding.get("className"))
            for binding in bindingList
            if isinstance(binding.get("className"), str)
            and binding.get("className") in current
        }
        if not boundNames:
            return current
        expanded = current | _joinNeighbors(joinList, boundNames)
        if expanded == current:
            return current
        current = expanded


def _joinNeighbors(
    joins: Iterable[dict[str, Any]], classNames: set[str]
) -> set[str]:
    """一跳 JOIN 邻居（含自己）：让「有没有边」的判据在切片与本体等价。"""
    neighbors: set[str] = set()
    for join in joins:
        source = join.get("sourceClassName")
        target = join.get("targetClassName")
        if not (isinstance(source, str) and isinstance(target, str)):
            continue
        if source in classNames or target in classNames:
            neighbors.add(source)
            neighbors.add(target)
    return neighbors


def _copyPartitions(
    items: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """逐条**深拷贝**：切片不与入参 manifest 共享对象。

    为什么要拷贝：切片会被原样存进快照正文（JSONB），而 JSONB **不做可变
    追踪** ⇒ 源行与副本行在内存里绑定同一个 dict ⇒ 任何就地改写都是
    **静默脏数据**（库里两行都不更新，内存里两行一起变）。
    """
    return tuple(copy.deepcopy(dict(item)) for item in items)


def _bindingSortKey(binding: Mapping[str, Any]) -> tuple[str, ...]:
    """绑定的稳定排序键（覆盖全部身份字段 ⇒ 并列时内容相同）。"""
    return (
        str(binding.get("className")),
        str(binding.get("datasourceName") or ""),
        str(binding.get("schemaName") or ""),
        str(binding.get("sourceTable")),
        str(binding.get("bindingRole")),
    )


def _domainEntries(
    manifest: Mapping[str, Any],
    domainCode: str,
    businessObjects: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """本域 + 切片内业务对象引用到的域（引用闸要求业务对象的域名必须存在）。"""
    wanted: set[str] = {domainCode}
    for bo in businessObjects:
        primary = bo.get("primaryDomainCode")
        if isinstance(primary, str):
            wanted.add(primary)
        attached = bo.get("attachedDomainCodes")
        if isinstance(attached, list):
            wanted.update(code for code in attached if isinstance(code, str))
    return [
        entry
        for entry in _partition(manifest, "domains")
        if isinstance(entry.get("code"), str) and entry.get("code") in wanted
    ]
