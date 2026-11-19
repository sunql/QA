"""发布域切片（M1-1.3 批次 B / D-10）：全域 manifest → 「这个域能发布的那一份」。

与检索切片（`test_knowledge_domain_slice.py`）的差别，正是批次 B 要解决的三件事：

1. **引用闭合补齐**：切片内指标引用到的类要补进来，否则快照过不了引用契约；
2. **JOIN 邻居闭���（取不动点）**：孤岛判据要在「切片」与「本体」两个口径上等价；
3. **绑定分区与域词表**：`sourceBindings` / `domains` 两段要跟着切片走。

⚠️ 最要紧的一条是**幂等**：发布快照存的就是切片，而校验项（如「跨域补齐了什么」）
必须能从**已存快照**复算 ⇒ 对切片再切一次必须得到同一份。
"""

from __future__ import annotations

from typing import Any

from app.domain.semantic_manifest_schema import SCHEMA_VERSION, validateManifest
from app.services.semantic_domain_slice import (
    sliceManifestForRelease,
    sliceToManifest,
)
from app.tests._knowledge_manifest import sampleManifest


def _withCrossDomainMetric() -> dict[str, Any]:
    """本域指标引用了**别的域**的类（⇒ 需要引用闭合补齐）。"""
    manifest = sampleManifest()
    manifest["metrics"][1]["sources"] = [
        {"className": "InspectionRecord", "role": "FILTER"}
    ]
    return manifest


def _binding(className: str, **overrides: Any) -> dict[str, Any]:
    binding = {
        "className": className,
        "datasourceName": "THBI",
        "schemaName": "ERP",
        "sourceTable": f"T_{className.upper()}",
        "bindingRole": "BASE",
        "filterExpression": None,
        "filterAllowlist": [],
        "properties": [],
    }
    binding.update(overrides)
    return binding


def _join(joinKey: str, source: str, target: str) -> dict[str, Any]:
    return {
        "joinKey": joinKey,
        "sourceClassName": source,
        "sourceColumns": ["ID"],
        "targetClassName": target,
        "targetColumns": ["ID"],
        "joinType": "INNER",
        "relationType": "foreign_key",
        "description": None,
    }


def _class(
    className: str,
    *,
    parent: str | None = None,
    properties: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "className": className,
        "classAlias": className,
        "description": None,
        "sourceTable": f"T_{className.upper()}",
        "objectType": "Master",
        "objectOwner": None,
        "parentClassName": parent,
        "properties": properties or [],
    }


def _property(name: str, refClassName: str | None = None) -> dict[str, Any]:
    """一个类属性（`refClassName` 非空 = 指向另一个类的字典型引用）。"""
    return {
        "propertyName": name,
        "propertyAlias": name,
        "businessAliases": [],
        "description": None,
        "dataType": "string",
        "isPrimaryKey": False,
        "isForeignKey": refClassName is not None,
        "refClassName": refClassName,
        "sourceColumn": name.upper(),
        "unit": None,
        "allowedValues": [],
        "isNotNull": None,
        "minValue": None,
        "maxValue": None,
        "regexPattern": None,
    }


# --- 切片内容 ----------------------------------------------------------------


def test_releaseSliceKeepsOnlyTheDomain() -> None:
    slice_ = sliceManifestForRelease(sampleManifest(), "PROCUREMENT")

    assert slice_.domainCode == "PROCUREMENT"
    assert "Supplier" in slice_.classNames
    assert "PurchaseOrder" in slice_.classNames
    assert "InspectionRecord" not in slice_.classNames


def test_releaseSlicePullsInMetricReferencedClasses() -> None:
    """本域指标引用了别的域的类 ⇒ 引用闭合补齐把它拉进切片。

    否则快照的引用契约会判「类不存在」⇒ **采购域永远发不出去**。
    """
    slice_ = sliceManifestForRelease(_withCrossDomainMetric(), "PROCUREMENT")

    assert "InspectionRecord" in slice_.classNames
    assert slice_.closureClassNames == ("InspectionRecord",)


def test_releaseSliceKeepsJoinsWhoseBothEndsAreInside() -> None:
    """补进来的类与本域类之间的 JOIN 同样进切片（图的连通性）。"""
    slice_ = sliceManifestForRelease(_withCrossDomainMetric(), "PROCUREMENT")

    assert {join["joinKey"] for join in slice_.joins} == {"po_supplier", "iqc_po"}


def test_releaseSlicePullsInJoinNeighboursOfBoundClasses() -> None:
    """有绑定的类 ⇒ 它的一跳 JOIN 邻居也进切片（孤岛判据要口径等价）。

    没有这一步的话：「只连跨域边」的类在切片里看着像孤岛 ⇒ 误阻断合法发布。
    """
    manifest = sampleManifest()
    manifest["sourceBindings"] = [_binding("PurchaseOrder")]
    manifest["joins"].append(_join("po_iqc", "PurchaseOrder", "InspectionRecord"))

    slice_ = sliceManifestForRelease(manifest, "PROCUREMENT")

    assert "InspectionRecord" in slice_.classNames
    assert "InspectionRecord" in slice_.closureClassNames


def test_releaseSliceJoinClosureReachesFixedPoint() -> None:
    """不动点：邻居若自己也有绑定，它的邻居同样要补。

    链条 A(有绑定) — B — C(有绑定)：B 被拉进来之后，B 自己也是绑定类
    ⇒ C 也要进来。否则切片里会出现「类在、边不在」的类 —— 自相矛盾的图。
    """
    manifest = sampleManifest()
    manifest["classes"].append(_class("QualityLot"))
    manifest["joins"].append(_join("po_iqc", "PurchaseOrder", "InspectionRecord"))
    manifest["joins"].append(_join("iqc_lot", "InspectionRecord", "QualityLot"))
    manifest["sourceBindings"] = [
        _binding("PurchaseOrder"),
        _binding("InspectionRecord"),
    ]

    slice_ = sliceManifestForRelease(manifest, "PROCUREMENT")

    assert "InspectionRecord" in slice_.classNames
    assert "QualityLot" in slice_.classNames
    assert "iqc_lot" in {join["joinKey"] for join in slice_.joins}


def test_releaseSliceKeepsOnlyBindingsInsideTheSlice() -> None:
    manifest = sampleManifest()
    manifest["sourceBindings"] = [_binding("Supplier"), _binding("InspectionRecord")]

    slice_ = sliceManifestForRelease(manifest, "PROCUREMENT")

    assert [binding["className"] for binding in slice_.sourceBindings] == ["Supplier"]


def test_releaseSliceKeepsDomainEntriesForReferencedDomains() -> None:
    """`domains` 分区要含本域 + 切片内业务对象引用到的域（引用闸要求）。"""
    slice_ = sliceManifestForRelease(sampleManifest(), "PROCUREMENT")

    assert [entry["code"] for entry in slice_.domains] == ["PROCUREMENT"]


def test_unknownDomainYieldsEmptyReleaseSlice() -> None:
    slice_ = sliceManifestForRelease(sampleManifest(), "NO_SUCH_DOMAIN")

    assert slice_.classNames == ()
    assert slice_.closureClassNames == ()


def test_releaseSlicePullsInPropertyReferencedClasses() -> None:
    """属性引用（`properties[].refClassName`）也必须闭进切片。

    v2 引用契约要求「属性的 refClassName 必须存在于切片里」⇒ 漏这一条 ⇒
    **该域永远发不出去**（而且是裸 ValueError ⇒ 500）。
    """
    manifest = sampleManifest()
    manifest["classes"][1]["properties"].append(
        _property("iqc_ref", refClassName="InspectionRecord")
    )

    slice_ = sliceManifestForRelease(manifest, "PROCUREMENT")

    assert "InspectionRecord" in slice_.classNames
    assert "InspectionRecord" in slice_.closureClassNames


def test_sliceWithPropertyReferencesPassesTheContract() -> None:
    """带属性引用的切片必须**过契约校验**（否则发不出去）。"""
    manifest = sampleManifest()
    manifest["classes"][1]["properties"].append(
        _property("iqc_ref", refClassName="InspectionRecord")
    )

    slice_ = sliceManifestForRelease(manifest, "PROCUREMENT")

    validateManifest(sliceToManifest(slice_, SCHEMA_VERSION))


def test_releaseSlicePullsInJoinNeighboursOfABoundParentClass() -> None:
    """**联合不动点**：补进来的类的父类若自己也有绑定，它的 JOIN 邻居也要补。

    形状：指标引用跨域类 X；X 的父类 P 有源绑定；P 只连 Q。
    若父类闭包与 JOIN 闭包**不是交替迭代**（先 JOIN 后父类），
    P 会被补进来但它的边不会 ⇒ 孤岛判据把 P 判成「零 JOIN 边」
    ⇒ **误阻断合法发布**（且报出的事实在事实上是错的）。
    """
    manifest = sampleManifest()
    manifest["classes"].append(_class("ParentP"))
    manifest["classes"].append(_class("ChildX", parent="ParentP"))
    manifest["classes"].append(_class("NeighbourQ"))
    manifest["joins"].append(_join("p_q", "ParentP", "NeighbourQ"))
    manifest["sourceBindings"] = [_binding("ParentP")]
    manifest["metrics"][1]["sources"] = [
        {"className": "ChildX", "role": "FILTER"}
    ]

    slice_ = sliceManifestForRelease(manifest, "PROCUREMENT")

    assert "ParentP" in slice_.classNames
    assert "NeighbourQ" in slice_.classNames
    assert "p_q" in {join["joinKey"] for join in slice_.joins}


def test_releaseSliceDoesNotShareDictsWithTheInputManifest() -> None:
    """切片与入参 manifest **不共享对象**（不可变数据铁律）。

    切片会被原样存进快照正文（JSONB 不做可变追踪）⇒ 共享 dict ⇒
    任何就地改写都是**静默脏数据**。
    """
    manifest = sampleManifest()
    manifest["sourceBindings"] = [_binding("Supplier")]

    slice_ = sliceManifestForRelease(manifest, "PROCUREMENT")
    slice_.classes[0]["className"] = "MUTATED"

    assert manifest["classes"][0]["className"] != "MUTATED"


# --- 幂等（可复算）----------------------------------------------------------


def test_releaseSliceIsIdempotent() -> None:
    """对切片再切一次 ⇒ 同一份（否则校验项无法从已存快照复算）。"""
    first = sliceManifestForRelease(_withCrossDomainMetric(), "PROCUREMENT")
    manifest = sliceToManifest(first, SCHEMA_VERSION)
    second = sliceManifestForRelease(manifest, "PROCUREMENT")

    assert second.classNames == first.classNames
    assert second.closureClassNames == first.closureClassNames
    assert [binding["className"] for binding in second.sourceBindings] == [
        binding["className"] for binding in first.sourceBindings
    ]


def _assertIdempotent(manifest: dict[str, Any]) -> None:
    """幂等判据：类集合 / 闭合集合 / 绑定集合 / JOIN 集合四件全等。"""
    first = sliceManifestForRelease(manifest, "PROCUREMENT")
    second = sliceManifestForRelease(sliceToManifest(first, SCHEMA_VERSION), "PROCUREMENT")

    assert second.classNames == first.classNames
    assert second.closureClassNames == first.closureClassNames
    assert {join["joinKey"] for join in second.joins} == {
        join["joinKey"] for join in first.joins
    }
    assert [binding["className"] for binding in second.sourceBindings] == [
        binding["className"] for binding in first.sourceBindings
    ]


def test_releaseSliceIsIdempotentOnTheJoinFixedPoint() -> None:
    """幂等（不动点链）：A(绑定)—B(绑定)—C。

    这条最容易被漏：B 被拉进来后**自己也是绑定类** ⇒ C 也要进来；
    而第二轮切片时 B 已不在「业务对象派生」的类集合里 ⇒ 若闭包不是幂等的，
    C 会在第二轮**掉出切片** ⇒ 「跨域补齐了什么」的校验项在 validate 阶段
    复算不出与草稿阶段同一个结论（证据不可复算）。
    """
    manifest = sampleManifest()
    manifest["classes"].append(_class("QualityLot"))
    manifest["joins"].append(_join("po_iqc", "PurchaseOrder", "InspectionRecord"))
    manifest["joins"].append(_join("iqc_lot", "InspectionRecord", "QualityLot"))
    manifest["sourceBindings"] = [
        _binding("PurchaseOrder"),
        _binding("InspectionRecord"),
    ]

    _assertIdempotent(manifest)


def test_releaseSliceIsIdempotentWhenABoundClassIsOnlyReachedByAMetric() -> None:
    """幂等（绑定类只被指标引用）：该类**没有 JOIN 邻居**，只能靠度量引用进场。

    这是最容易被误判成「不可复算」的形状：第二轮切片时它既不是业务对象派生类、
    也不是 JOIN 邻居，只剩「被本域指标引用」这一条路 ⇒ 该路必须仍然通。
    """
    manifest = sampleManifest()
    manifest["classes"].append(_class("OnlyByMetric"))
    manifest["metrics"][1]["sources"] = [
        {"className": "OnlyByMetric", "role": "FILTER"}
    ]
    manifest["sourceBindings"] = [_binding("OnlyByMetric")]

    _assertIdempotent(manifest)


def test_releaseSliceIsIdempotentWithPropertyReferences() -> None:
    """幂等（属性引用）：补进来的类第二轮仍在切片里。

    属性引用闭包必须是**幂等**的：第二轮切片时属性引用的目标类已在切片里
    ⇒ 集合不变 ⇒ 校验项可从已存快照复算。
    """
    manifest = sampleManifest()
    manifest["classes"][1]["properties"].append(
        _property("iqc_ref", refClassName="InspectionRecord")
    )

    _assertIdempotent(manifest)


def test_releaseSliceIsIdempotentOnTheBoundParentFixedPoint() -> None:
    """幂等（联合不动点：绑定父类的 JOIN 邻居）。"""
    manifest = sampleManifest()
    manifest["classes"].append(_class("ParentP"))
    manifest["classes"].append(_class("ChildX", parent="ParentP"))
    manifest["classes"].append(_class("NeighbourQ"))
    manifest["joins"].append(_join("p_q", "ParentP", "NeighbourQ"))
    manifest["sourceBindings"] = [_binding("ParentP")]
    manifest["metrics"][1]["sources"] = [
        {"className": "ChildX", "role": "FILTER"}
    ]

    _assertIdempotent(manifest)


def test_sliceToManifestPassesTheContract() -> None:
    """切片还原成的 manifest 必须**过契约校验**（否则发不出去）。"""
    manifest = sampleManifest()
    manifest["sourceBindings"] = [_binding("Supplier")]
    slice_ = sliceManifestForRelease(manifest, "PROCUREMENT")

    validateManifest(sliceToManifest(slice_, SCHEMA_VERSION))


def test_sliceToManifestKeepsPartitionOrderStable() -> None:
    """分区顺序固定 ⇒ 同一治理内容两次还原出同样的字节。"""
    slice_ = sliceManifestForRelease(sampleManifest(), "PROCUREMENT")

    manifest = sliceToManifest(slice_, SCHEMA_VERSION)

    assert list(manifest.keys()) == [
        "schemaVersion",
        "domains",
        "classes",
        "joins",
        "metrics",
        "kpis",
        "businessObjects",
        "sourceBindings",
    ]


def test_sliceToManifestCanOmitSourceBindings() -> None:
    """v1 契约没有绑定分区 ⇒ 还原时不能带它。"""
    slice_ = sliceManifestForRelease(sampleManifest(), "PROCUREMENT")

    manifest = sliceToManifest(slice_, SCHEMA_VERSION, includeSourceBindings=False)

    assert "sourceBindings" not in manifest
