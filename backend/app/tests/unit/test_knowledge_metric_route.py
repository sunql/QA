"""P4 指标路（METRIC）：从**发布快照**召回指标 / KPI。

判据要点
--------
1. 指标与 KPI 都**按发布读**（证据带 `releaseId` / `releaseVersion`）。
2. **只在本域内召回**（域切片已保证）⇒ 别的域的指标不进结果。
3. **空集正确返回**（`EMPTY` + 0 条），不是报错、不是降级。
4. 指标与 KPI **分别**按自己的词项召回（KPI 可被 `semanticKeywords` 命中，
   不要求它所属的指标同时命中）。
5. **统一输入**：四路拿同一个 `RouteInput`（同一份发布面 + 同一份权限口径）。
"""

from __future__ import annotations

from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_METRIC,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_RELEASE,
)
from app.services.knowledge.metric_route import runMetricRoute
from app.services.knowledge.permission import KnowledgeScope
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_input import RouteInput
from app.tests._knowledge_manifest import sampleManifest


def _release() -> ResolvedRelease:
    return ResolvedRelease(
        manifest=sampleManifest(),
        release_id=9,
        version="2026.11.16-2",
        manifest_hash="b" * 64,
        degraded=False,
        reason=None,
    )


def _degradedRelease() -> ResolvedRelease:
    return ResolvedRelease(
        manifest=None,
        release_id=None,
        version=None,
        manifest_hash=None,
        degraded=True,
        reason="该域当前没有已激活的语义发布",
    )


def _scope() -> KnowledgeScope:
    return KnowledgeScope(
        domain_codes=("PROCUREMENT",),
        org_codes=("PROCUREMENT_ORG",),
        entity_types=("SUPPLIER",),
        max_security_level="L1",
        is_admin=False,
    )


def _input(
    question: str,
    *,
    release: ResolvedRelease | None = None,
    top_k: int = 8,
) -> RouteInput:
    return RouteInput(
        question=question,
        domain_code="PROCUREMENT",
        release=release if release is not None else _release(),
        scope=_scope(),
        top_k=top_k,
    )


async def test_metricRouteMatchesMetricByChineseAlias() -> None:
    outcome = await runMetricRoute(_input("供应商交付天数"))

    assert outcome.channel == KNOWLEDGE_CHANNEL_METRIC
    assert outcome.status == KNOWLEDGE_ROUTE_OK
    metricEvidence = [item for item in outcome.evidence if item.kind == "METRIC"]
    assert [item.ref for item in metricEvidence] == ["supplier_delivery_days"]
    assert metricEvidence[0].title == "供应商交付天数"


async def test_metricRouteStampsReleaseVersionOnEvidence() -> None:
    outcome = await runMetricRoute(_input("供应商交付天数"))

    assert outcome.evidence
    for item in outcome.evidence:
        assert item.release_id == 9
        assert item.release_version == "2026.11.16-2"
        assert item.source == KNOWLEDGE_SOURCE_RELEASE


async def test_metricRouteMatchesKpiByName() -> None:
    outcome = await runMetricRoute(_input("供应商交付及时率"))

    kpiEvidence = [item for item in outcome.evidence if item.kind == "KPI"]
    assert [item.ref for item in kpiEvidence] == ["KPI_DELIVERY"]


async def test_metricRouteMatchesKpiBySemanticKeyword() -> None:
    """KPI 的语义关键词（`semanticKeywords`）也是召回入口。"""
    outcome = await runMetricRoute(_input("及时率"))

    kpiEvidence = [item for item in outcome.evidence if item.kind == "KPI"]
    assert [item.ref for item in kpiEvidence] == ["KPI_DELIVERY"]


async def test_metricRouteExcludesOtherDomainMetrics() -> None:
    outcome = await runMetricRoute(_input("来料合格率"))

    assert outcome.status == KNOWLEDGE_ROUTE_EMPTY
    assert outcome.evidence == ()


async def test_metricRouteReturnsEmptyWhenNothingMatches() -> None:
    outcome = await runMetricRoute(_input("今天天气怎么样"))

    assert outcome.status == KNOWLEDGE_ROUTE_EMPTY
    assert outcome.evidence == ()
    assert outcome.degraded_reason is None


async def test_metricRouteDegradesWhenReleaseUnavailable() -> None:
    outcome = await runMetricRoute(
        _input("供应商交付天数", release=_degradedRelease())
    )

    assert outcome.status == KNOWLEDGE_ROUTE_DEGRADED
    assert outcome.evidence == ()
    assert outcome.degraded_reason == "该域当前没有已激活的语义发布"


async def test_metricRouteRespectsTopK() -> None:
    outcome = await runMetricRoute(
        _input("供应商交付天数 采购订单金额", top_k=1)
    )

    assert len(outcome.evidence) == 1


async def test_metricRouteIsDeterministic() -> None:
    first = await runMetricRoute(_input("供应商交付天数 及时率"))
    second = await runMetricRoute(_input("供应商交付天数 及时率"))

    assert [item.ref for item in first.evidence] == [
        item.ref for item in second.evidence
    ]
