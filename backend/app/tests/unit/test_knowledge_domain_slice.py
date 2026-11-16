"""P4 域切片：发布 manifest → **某个域**能看到的那部分条目。

为什么需要这一层
----------------
发布快照是**全域**的（一次编译把治理表全量编进 manifest），而一次知识查询只问
一个域。⇒ 必须有一层「按域切片」，否则问采购会把质量域的类一起召回
⇒ 跨域可用的判据就变成了「换个 domain 参数，结果不变」。

切片口径（manifest 里**没有** class↔domain 的直连字段，只有业务对象挂域）
------------------------------------------------------------------------
1. **业务对象**：`primaryDomainCode == 域` 或 `attachedDomainCodes` 含该域。
2. **类**：业务对象的 `headerClassName` + 它们的**父类闭包**（结构性依赖）。
3. **JOIN**：两端都在类集合里（跨域的 JOIN 是边界，不进切片）。
4. **指标**：`targetClassName` / `numeratorMeasure.className` /
   `denominatorMeasure.className` / `sources[].className` 命中类集合。
5. **KPI**：`metricName` 命中本域指标，或 `kpiCode` 出现在本域指标的 `kpiCodes` 里。

⚠️ 归不了属的条目（无域锚点）**一律排除**（fail-closed）：宁可少给，
也不把别的域的东西端上来。
"""

from __future__ import annotations

from app.services.knowledge.domain_slice import sliceManifestByDomain
from app.tests._knowledge_manifest import sampleManifest


def test_sliceKeepsOnlyTheRequestedDomain() -> None:
    slice_ = sliceManifestByDomain(sampleManifest(), "PROCUREMENT")

    assert slice_.domainCode == "PROCUREMENT"
    assert set(slice_.classNames) == {"Supplier", "PurchaseOrder"}
    assert "InspectionRecord" not in slice_.classNames


def test_sliceIncludesParentClassClosure() -> None:
    """PurchaseOrder 的父类是 Supplier ⇒ 父类进切片（结构性依赖）。"""
    slice_ = sliceManifestByDomain(sampleManifest(), "PROCUREMENT")

    assert "Supplier" in slice_.classNames


def test_sliceIncludesAttachedDomainBusinessObjects() -> None:
    """附域业务对象也算该域可见。"""
    manifest = sampleManifest()
    manifest["businessObjects"] = [
        {
            "code": "INSPECTION_RECORD",
            "name": "检验记录",
            "headerClassName": "InspectionRecord",
            "primaryDomainCode": "QUALITY",
            "attachedDomainCodes": ["PROCUREMENT"],
            "graphLabel": "InspectionRecord",
            "description": None,
        }
    ]

    slice_ = sliceManifestByDomain(manifest, "PROCUREMENT")

    assert "InspectionRecord" in slice_.classNames


def test_sliceKeepsOnlyJoinsWhoseBothEndsAreInside() -> None:
    """跨域 JOIN 是边界 ⇒ 不进切片。"""
    slice_ = sliceManifestByDomain(sampleManifest(), "PROCUREMENT")

    assert [join["joinKey"] for join in slice_.joins] == ["po_supplier"]


def test_sliceAttributesMetricsByTargetClass() -> None:
    slice_ = sliceManifestByDomain(sampleManifest(), "PROCUREMENT")

    assert [metric["metricName"] for metric in slice_.metrics] == [
        "po_amount",
        "supplier_delivery_days",
    ]


def test_sliceAttributesMetricsByMeasureSourceClass() -> None:
    """指标没有 targetClassName 时，看 numerator / denominator 的类。"""
    manifest = sampleManifest()
    manifest["metrics"] = [
        {
            **manifest["metrics"][1],
            "targetClassName": None,
            "numeratorMeasure": {
                "agg": "SUM",
                "expr": None,
                "filter": None,
                "className": "PurchaseOrder",
                "classAlias": None,
            },
        }
    ]

    slice_ = sliceManifestByDomain(manifest, "PROCUREMENT")

    assert [metric["metricName"] for metric in slice_.metrics] == ["po_amount"]


def test_sliceAttributesKpisByMetricName() -> None:
    slice_ = sliceManifestByDomain(sampleManifest(), "PROCUREMENT")

    assert [kpi["kpiCode"] for kpi in slice_.kpis] == [
        "KPI_DELIVERY",
        "KPI_PO_AMOUNT",
    ]


def test_sliceDropsKpiWhoseMetricIsOutOfDomain() -> None:
    """KPI 指向别的域的指标 ⇒ 不进切片（fail-closed）。"""
    manifest = sampleManifest()
    manifest["metrics"][0]["kpiCodes"] = []
    manifest["kpis"][0]["metricName"] = "iqc_pass_rate"

    slice_ = sliceManifestByDomain(manifest, "PROCUREMENT")

    assert "KPI_DELIVERY" not in [kpi["kpiCode"] for kpi in slice_.kpis]


def test_sliceKeepsKpiListedInDomainMetricKpiCodes() -> None:
    """反向映射：域内指标的 `kpiCodes` 里点名的 KPI 同样进切片。

    （正式 manifest 里 `metrics[].kpiCodes` 与 `kpis[].metricName` 是双向闭合的，
    这里刻意用一份**不闭合**的快照验证切片不依赖单一方向。）
    """
    manifest = sampleManifest()
    manifest["kpis"][0]["metricName"] = None

    slice_ = sliceManifestByDomain(manifest, "PROCUREMENT")

    assert "KPI_DELIVERY" in [kpi["kpiCode"] for kpi in slice_.kpis]


def test_unknownDomainYieldsEmptySlice() -> None:
    """未知域 ⇒ 空切片（不是「全量」也不是报错）。"""
    slice_ = sliceManifestByDomain(sampleManifest(), "NO_SUCH_DOMAIN")

    assert slice_.classNames == ()
    assert slice_.joins == ()
    assert slice_.metrics == ()
    assert slice_.kpis == ()


def test_emptyManifestYieldsEmptySlice() -> None:
    slice_ = sliceManifestByDomain({}, "PROCUREMENT")

    assert slice_.classNames == ()


# --- 请求侧收窄（`entityTypes`）------------------------------------------------


def test_sliceWithoutEntityTypesIsNotNarrowed() -> None:
    """`entityTypes=None`（没要收窄）⇒ 切片与不传时逐字一致。"""
    slice_ = sliceManifestByDomain(sampleManifest(), "PROCUREMENT", entityTypes=None)

    assert set(slice_.classNames) == {"Supplier", "PurchaseOrder"}


def test_sliceNarrowsToRequestedBusinessObjects() -> None:
    """收窄到某个业务对象 ⇒ 只剩它的类（及其父类闭包）与挂在它身上的指标。

    ⚠️ 父类闭包是**结构性依赖**（`PurchaseOrder` 的父类是 `Supplier`）
    ⇒ 父类上的指标（`supplier_delivery_days`）同样留在切片里 —— 这是切片的
    既定口径，不是收窄失效。
    """
    slice_ = sliceManifestByDomain(
        sampleManifest(), "PROCUREMENT", entityTypes=("PURCHASE_ORDER",)
    )

    assert set(slice_.classNames) == {"PurchaseOrder", "Supplier"}
    assert [metric["metricName"] for metric in slice_.metrics] == [
        "po_amount",
        "supplier_delivery_days",
    ]
    assert [bo["code"] for bo in slice_.businessObjects] == ["PURCHASE_ORDER"]


def test_sliceWithEmptyEntityTypesYieldsEmptySlice() -> None:
    """收窄到空 ⇒ **空切片**（不许回落成「全都给」）。"""
    slice_ = sliceManifestByDomain(sampleManifest(), "PROCUREMENT", entityTypes=())

    assert slice_.classNames == ()
    assert slice_.metrics == ()
    assert slice_.kpis == ()
    assert slice_.businessObjects == ()


def test_sliceIgnoresBusinessObjectsOutsideTheDomain() -> None:
    """收窄到一个**别的域**的业务对象 ⇒ 本域切片里没有它 ⇒ 空切片。"""
    slice_ = sliceManifestByDomain(
        sampleManifest(), "PROCUREMENT", entityTypes=("INSPECTION_RECORD",)
    )

    assert slice_.classNames == ()
