"""采购域发布校验项（M1-1.3 批次 B）：域切片 + 源绑定。

钉住四件事：

1. **空切片阻断**：域切片为空 ⇒ ERROR（不能发布一份空快照）。
2. **跨域补齐显式化**：补进来的类要出现在报告里（WARNING），不能悄悄发生。
3. **候选是治理债**：候选绑定不进快照，但必须被显式暴露（WARNING）；
   **没查过候选数就不出这一行**（不能写一句没依据的「没有候选」）。
4. **孤岛判据取本体口径**：绑定类在本体里零 JOIN 边 ⇒ ERROR（发布出去也走不通）。
"""

from __future__ import annotations

from typing import Any

from app.services.semantic_release_check_types import (
    RESULT_FAIL,
    RESULT_PASS,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    TARGET_BINDING,
    TARGET_RELEASE,
)
from app.services.semantic_release_domain_checks import (
    CHECK_DOMAIN_SLICE_CROSS_DOMAIN,
    CHECK_DOMAIN_SLICE_EMPTY,
    CHECK_SOURCE_BINDING_CANDIDATE,
    CHECK_SOURCE_BINDING_JOIN_MISSING,
    CHECK_SOURCE_BINDING_PROPERTY_UNBOUND,
    ReleaseCheckContext,
    evaluateDomainChecks,
)
from app.tests._knowledge_manifest import sampleManifest


def _byCode(checks: tuple[Any, ...], code: str) -> list[Any]:
    return [check for check in checks if check.code == code]


def _classWithoutJoins(className: str) -> dict[str, Any]:
    """一个**零 JOIN 边**的类（孤岛）：用来验孤岛判据。"""
    return {
        "className": className,
        "classAlias": className,
        "description": None,
        "sourceTable": f"T_{className.upper()}",
        "objectType": "Master",
        "objectOwner": None,
        "parentClassName": None,
        "properties": [],
    }


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


# --- 域切片 ----------------------------------------------------------------


def test_unknownDomainYieldsEmptySliceErrorRow() -> None:
    checks = evaluateDomainChecks(
        sampleManifest(),
        context=ReleaseCheckContext(domainCode="NO_SUCH_DOMAIN"),
    )

    rows = _byCode(checks, CHECK_DOMAIN_SLICE_EMPTY)
    assert len(rows) == 1
    assert rows[0].result == RESULT_FAIL
    assert rows[0].severity == SEVERITY_ERROR
    assert rows[0].targetType == TARGET_RELEASE


def test_healthyDomainYieldsNoEmptySliceRow() -> None:
    checks = evaluateDomainChecks(
        sampleManifest(),
        context=ReleaseCheckContext(domainCode="PROCUREMENT"),
    )

    assert _byCode(checks, CHECK_DOMAIN_SLICE_EMPTY) == []


def test_crossDomainClosureIsExposedAsWarning() -> None:
    """本域指标引用了别的域的类 ⇒ 补齐 ⇒ 报告里要看得见。"""
    manifest = sampleManifest()
    manifest["metrics"][1]["sources"] = [
        {"className": "InspectionRecord", "role": "FILTER"}
    ]

    checks = evaluateDomainChecks(
        manifest, context=ReleaseCheckContext(domainCode="PROCUREMENT")
    )

    rows = _byCode(checks, CHECK_DOMAIN_SLICE_CROSS_DOMAIN)
    assert len(rows) == 1
    assert rows[0].result == RESULT_FAIL
    assert rows[0].severity == SEVERITY_WARNING
    assert "InspectionRecord" in rows[0].detail


def test_domainWithoutCrossDomainClosurePasses() -> None:
    checks = evaluateDomainChecks(
        sampleManifest(),
        context=ReleaseCheckContext(domainCode="PROCUREMENT"),
    )

    rows = _byCode(checks, CHECK_DOMAIN_SLICE_CROSS_DOMAIN)
    assert len(rows) == 1
    assert rows[0].result == RESULT_PASS


def test_globalReleaseHasNoDomainSliceRows() -> None:
    """全局发布没有域 ⇒ 不做域切片校验（否则会误判成「空切片」）。"""
    checks = evaluateDomainChecks(sampleManifest(), context=ReleaseCheckContext())

    assert _byCode(checks, CHECK_DOMAIN_SLICE_EMPTY) == []
    assert _byCode(checks, CHECK_DOMAIN_SLICE_CROSS_DOMAIN) == []


# --- 候选绑定 --------------------------------------------------------------


def test_candidateBindingsAreReportedAsWarning() -> None:
    checks = evaluateDomainChecks(
        sampleManifest(),
        context=ReleaseCheckContext(candidateBindingCount=3, candidateClassNames=("A", "B")),
    )

    rows = _byCode(checks, CHECK_SOURCE_BINDING_CANDIDATE)
    assert len(rows) == 1
    assert rows[0].result == RESULT_FAIL
    assert rows[0].severity == SEVERITY_WARNING
    assert "3" in rows[0].detail


def test_noCandidateBindingsPasses() -> None:
    checks = evaluateDomainChecks(
        sampleManifest(), context=ReleaseCheckContext(candidateBindingCount=0)
    )

    rows = _byCode(checks, CHECK_SOURCE_BINDING_CANDIDATE)
    assert len(rows) == 1
    assert rows[0].result == RESULT_PASS


def test_unknownCandidateCountProducesNoRow() -> None:
    """没查过候选数 ⇒ **不出这一行**（不写没依据的结论）。"""
    checks = evaluateDomainChecks(sampleManifest(), context=ReleaseCheckContext())

    assert _byCode(checks, CHECK_SOURCE_BINDING_CANDIDATE) == []


# --- 绑定级 ----------------------------------------------------------------


def test_bindingClassWithoutJoinEdgeIsBlocked() -> None:
    """绑定类在整个本体里零 JOIN 边 ⇒ ERROR（发布出去也走不通）。"""
    manifest = sampleManifest()
    manifest["classes"].append(_classWithoutJoins("QualityLot"))
    manifest["sourceBindings"] = [_binding("QualityLot")]

    checks = evaluateDomainChecks(manifest, context=ReleaseCheckContext())

    rows = _byCode(checks, CHECK_SOURCE_BINDING_JOIN_MISSING)
    assert len(rows) == 1
    assert rows[0].result == RESULT_FAIL
    assert rows[0].severity == SEVERITY_ERROR
    assert rows[0].targetType == TARGET_BINDING


def test_bindingClassWithJoinEdgePasses() -> None:
    manifest = sampleManifest()
    manifest["sourceBindings"] = [_binding("PurchaseOrder")]

    checks = evaluateDomainChecks(manifest, context=ReleaseCheckContext())

    rows = _byCode(checks, CHECK_SOURCE_BINDING_JOIN_MISSING)
    assert len(rows) == 1
    assert rows[0].result == RESULT_PASS


def test_bindingTargetKeyCarriesDatasourceAndRole() -> None:
    """定位键必须含**数据源**与**角色** ⇒ 否则同表两角色撞唯一索引（500）。"""
    manifest = sampleManifest()
    manifest["sourceBindings"] = [
        _binding("PurchaseOrder", bindingRole="BASE"),
        _binding("PurchaseOrder", bindingRole="NUMERATOR"),
    ]

    checks = evaluateDomainChecks(manifest, context=ReleaseCheckContext())

    keys = {row.targetKey for row in _byCode(checks, CHECK_SOURCE_BINDING_JOIN_MISSING)}
    assert len(keys) == 2


def test_sameTableInTwoDatasourcesGetsDistinctTargetKeys() -> None:
    manifest = sampleManifest()
    manifest["sourceBindings"] = [
        _binding("PurchaseOrder", datasourceName="THBI"),
        _binding("PurchaseOrder", datasourceName="ERP2"),
    ]

    checks = evaluateDomainChecks(manifest, context=ReleaseCheckContext())

    keys = {row.targetKey for row in _byCode(checks, CHECK_SOURCE_BINDING_JOIN_MISSING)}
    assert len(keys) == 2


def test_unboundPropertyIsReportedAsWarning() -> None:
    """类声明了源列、但绑定没映射 ⇒ WARNING。"""
    manifest = sampleManifest()
    manifest["sourceBindings"] = [_binding("Supplier")]

    checks = evaluateDomainChecks(manifest, context=ReleaseCheckContext())

    rows = _byCode(checks, CHECK_SOURCE_BINDING_PROPERTY_UNBOUND)
    assert len(rows) == 1
    assert rows[0].result == RESULT_FAIL
    assert rows[0].severity == SEVERITY_WARNING


def test_fullyBoundClassHasNoUnboundPropertyRow() -> None:
    manifest = sampleManifest()
    manifest["sourceBindings"] = [
        _binding(
            "Supplier",
            properties=[
                {
                    "propertyName": "delivery_days",
                    "sourceColumn": "DELIVERY_DAYS",
                    "keyOrdinal": None,
                    "transformExpression": None,
                },
                {
                    "propertyName": "supplier_name",
                    "sourceColumn": "SUPPLIER_NAME",
                    "keyOrdinal": None,
                    "transformExpression": None,
                },
            ],
        )
    ]

    checks = evaluateDomainChecks(manifest, context=ReleaseCheckContext())

    rows = _byCode(checks, CHECK_SOURCE_BINDING_PROPERTY_UNBOUND)
    assert len(rows) == 1
    assert rows[0].result == RESULT_PASS


def test_manifestWithoutBindingsHasNoBindingRows() -> None:
    checks = evaluateDomainChecks(sampleManifest(), context=ReleaseCheckContext())

    assert _byCode(checks, CHECK_SOURCE_BINDING_JOIN_MISSING) == []
    assert _byCode(checks, CHECK_SOURCE_BINDING_PROPERTY_UNBOUND) == []


def test_multipleBindingsOfOneClassShareTheMappedProperties() -> None:
    """同一类的**多条绑定**分摊映射 ⇒ 不是债（假阳性）。

    判据若只看「这一条绑定」，则「绑定 T1 映射 a、绑定 T2 映射 b」的类
    会被两条绑定各报一次「未绑列」——而类其实已经绑全（多落点是合法设计）。
    """
    manifest = sampleManifest()
    manifest["sourceBindings"] = [
        _binding(
            "Supplier",
            sourceTable="T_SUPPLIER_A",
            properties=[
                {
                    "propertyName": "delivery_days",
                    "sourceColumn": "DELIVERY_DAYS",
                    "keyOrdinal": None,
                    "transformExpression": None,
                }
            ],
        ),
        _binding(
            "Supplier",
            sourceTable="T_SUPPLIER_B",
            properties=[
                {
                    "propertyName": "supplier_name",
                    "sourceColumn": "SUPPLIER_NAME",
                    "keyOrdinal": None,
                    "transformExpression": None,
                }
            ],
        ),
    ]

    checks = evaluateDomainChecks(manifest, context=ReleaseCheckContext())

    rows = _byCode(checks, CHECK_SOURCE_BINDING_PROPERTY_UNBOUND)
    assert len(rows) == 1, rows
    assert rows[0].result == RESULT_PASS
