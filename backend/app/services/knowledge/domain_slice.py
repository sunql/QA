"""P4 域切片：发布 manifest → **某个域**能看到的那部分条目。

为什么必须有这一层
------------------
发布快照是**全域**的（一次编译把治理表全量编进 manifest），而一次知识查询只问
一个域。⇒ 没有切片，问采购会把质量域的类一起召回 ⇒ 「跨域可用」就退化成
「换个 domain 参数结果不变」。

切片口径（manifest 里**没有** class↔domain 的直连字段，只有业务对象挂域）
------------------------------------------------------------------------
1. **业务对象**：`primaryDomainCode == 域` 或 `attachedDomainCodes` 含该域。
2. **类**：业务对象的 `headerClassName` + 它们的**父类闭包**（结构性依赖）。
3. **JOIN**：两端都在类集合里（跨域 JOIN 是边界，不进切片）。
4. **指标**：`targetClassName` / `numeratorMeasure.className` /
   `denominatorMeasure.className` / `sources[].className` 命中类集合。
5. **KPI**：`metricName` 命中本域指标，或 `kpiCode` 出现在本域指标的 `kpiCodes`。
6. **请求侧收窄**：`entityTypes` 非 `None` ⇒ 业务对象只保留这些（切片的第二道收窄；
   `()` ⇒ 空切片，**不许**回落成「全都给」）。

⚠️ 归不了属的条目（无域锚点）**一律排除**（fail-closed）：宁可少给，
也不把别的域的东西端上来。未知域 ⇒ **空切片**（不是「全量」，也不是报错）。

本模块是**纯函数**（不吃 DB、不吃 session）⇒ 可直接单测。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DomainSlice:
    """某个域在发布快照里对应的条目集合。"""

    domainCode: str
    classNames: tuple[str, ...]
    classes: tuple[dict[str, Any], ...]
    joins: tuple[dict[str, Any], ...]
    businessObjects: tuple[dict[str, Any], ...]
    metrics: tuple[dict[str, Any], ...]
    kpis: tuple[dict[str, Any], ...]


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


def sliceManifestByDomain(
    manifest: Mapping[str, Any],
    domainCode: str,
    *,
    entityTypes: Sequence[str] | None = None,
) -> DomainSlice:
    """把发布 manifest 切成某个域的切片。

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
