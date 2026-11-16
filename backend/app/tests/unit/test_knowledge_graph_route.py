"""P4 图谱路（GRAPH）：发布快照里的**类 / 属性 / JOIN** 召回。

判据要点
--------
1. **按发布读**：证据必须带 `releaseId` + `releaseVersion` + `source=RELEASE`
   （否则「引用可回溯到发布版本」这条 G5 判据无从判定）。
2. **空集正确返回**：没命中就是 `EMPTY` + 0 条，**不是**报错、不是 `DEGRADED`
   （D2 的「文档/空集也要正确返回」口径）。
3. **发布面不可用 ⇒ 显式降级**：`DEGRADED` + 降级原因，**不许假装是发布读**。
4. **只在本域内召回**：别的域的类不进结果。
5. **统一输入**：四路拿同一个 `RouteInput`（同一份发布面 + 同一份权限口径）。
"""

from __future__ import annotations

from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_GRAPH,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_RELEASE,
)
from app.services.knowledge.graph_route import runGraphRoute
from app.services.knowledge.permission import KnowledgeScope
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_input import RouteInput
from app.tests._knowledge_manifest import sampleManifest


def _release() -> ResolvedRelease:
    return ResolvedRelease(
        manifest=sampleManifest(),
        release_id=7,
        version="2026.11.16-1",
        manifest_hash="a" * 64,
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


async def test_graphRouteMatchesClassByChineseAlias() -> None:
    outcome = await runGraphRoute(_input("交付延迟最高的供应商？"))

    assert outcome.channel == KNOWLEDGE_CHANNEL_GRAPH
    assert outcome.status == KNOWLEDGE_ROUTE_OK
    classEvidence = [item for item in outcome.evidence if item.kind == "CLASS"]
    assert [item.ref for item in classEvidence] == ["Supplier"]
    assert classEvidence[0].title == "供应商"


async def test_graphRouteStampsReleaseVersionOnEvidence() -> None:
    """引用必须能回溯到发布版本。"""
    outcome = await runGraphRoute(_input("供应商"))

    assert outcome.evidence
    for item in outcome.evidence:
        assert item.release_id == 7
        assert item.release_version == "2026.11.16-1"
        assert item.source == KNOWLEDGE_SOURCE_RELEASE


async def test_graphRouteEmitsPropertyEvidence() -> None:
    outcome = await runGraphRoute(_input("供应商 交付天数"))

    propertyEvidence = [item for item in outcome.evidence if item.kind == "PROPERTY"]
    assert [item.ref for item in propertyEvidence] == ["Supplier.delivery_days"]


async def test_graphRouteEmitsJoinEvidenceBetweenMatchedClasses() -> None:
    outcome = await runGraphRoute(_input("采购订单 供应商"))

    joinEvidence = [item for item in outcome.evidence if item.kind == "JOIN"]
    assert [item.ref for item in joinEvidence] == ["po_supplier"]


async def test_graphRouteExcludesOtherDomainClasses() -> None:
    """质量域的类不进采购域的结果。"""
    outcome = await runGraphRoute(_input("检验记录"))

    assert outcome.status == KNOWLEDGE_ROUTE_EMPTY
    assert outcome.evidence == ()


async def test_graphRouteReturnsEmptyWhenNothingMatches() -> None:
    outcome = await runGraphRoute(_input("今天天气怎么样"))

    assert outcome.status == KNOWLEDGE_ROUTE_EMPTY
    assert outcome.evidence == ()
    assert outcome.degraded_reason is None


async def test_graphRouteDegradesWhenReleaseUnavailable() -> None:
    outcome = await runGraphRoute(_input("供应商", release=_degradedRelease()))

    assert outcome.status == KNOWLEDGE_ROUTE_DEGRADED
    assert outcome.evidence == ()
    assert outcome.degraded_reason == "该域当前没有已激活的语义发布"


async def test_graphRouteRespectsTopK() -> None:
    outcome = await runGraphRoute(_input("采购订单 供应商", top_k=1))

    assert len(outcome.evidence) == 1


async def test_graphRouteIsDeterministic() -> None:
    """同一问题重跑 ⇒ 同一批证据（顺序也一致）。"""
    first = await runGraphRoute(_input("采购订单 供应商 交付天数"))
    second = await runGraphRoute(_input("采购订单 供应商 交付天数"))

    assert [item.ref for item in first.evidence] == [
        item.ref for item in second.evidence
    ]
