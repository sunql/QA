"""P4 RAG 路（RAG）：向量召回（**按发布 + 按权限**收窄）。

判据要点
--------
1. **收窄必须落到过滤器上**：发布 id / 域 / 组织 / 业务对象，全部取自编排层给的
   同一份 `KnowledgeScope` 与 `ResolvedRelease` ⇒ 「四路同一口径」在 RAG 路可验。
2. **开关关 ⇒ fail-closed**：P3a 的口径是「声明了作用域却读不到按维度过滤的
   结果 ⇒ 拒绝」，**不许**回落成不过滤的召回 ⇒ 本路显式降级，且**不发起向量调用**。
3. **引用可回溯**：命中的向量行出自那一版发布 ⇒ 证据带 `releaseId` / `releaseVersion`。
4. **空集正确返回**：没命中 ⇒ `EMPTY`（闸门给了降级原因 ⇒ `DEGRADED`，两者分开）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_RAG,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_RELEASE,
)
from app.services.knowledge import rag_route
from app.services.knowledge.permission import KnowledgeScope
from app.services.knowledge.rag_route import runRagRoute
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_input import RouteInput
from app.services.semantic_registry_rag_gate import RegistrySearchResult
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
    question: str = "供应商",
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


def _hits(count: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "ontology_id": 100 + index,
            "type": "class",
            "name": f"Class{index}",
            "alias": f"别名{index}",
            "description": f"描述{index}",
            "distance": 0.1 + index * 0.01,
        }
        for index in range(count)
    ]


class _Recorder:
    """记录调用参数（替身只记录，不替身断言）。"""

    def __init__(self, result: RegistrySearchResult) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> RegistrySearchResult:
        self.calls.append(args)
        return self.result


async def _embedding(text: str) -> list[float]:
    return [0.1, 0.2, 0.3]


async def test_ragRouteReturnsEvidenceTraceableToRelease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """命中 ⇒ 证据带发布版本（引用可回溯）。"""
    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: True)
    searcher = _Recorder(RegistrySearchResult(hits=_hits(2), usedRegistryV2=True))

    outcome = await runRagRoute(_input(), embeddingGenerator=_embedding, searcher=searcher)

    assert outcome.channel == KNOWLEDGE_CHANNEL_RAG
    assert outcome.status == KNOWLEDGE_ROUTE_OK
    assert len(outcome.evidence) == 2
    for item in outcome.evidence:
        assert item.release_id == 7
        assert item.release_version == "2026.11.16-1"
        assert item.source == KNOWLEDGE_SOURCE_RELEASE
    assert outcome.entities


async def test_ragRouteFiltersByReleaseAndScope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过滤器必须带上**同一份**发布面与权限口径（收窄的硬证据）。"""
    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: True)
    searcher = _Recorder(RegistrySearchResult(hits=_hits(1), usedRegistryV2=True))

    await runRagRoute(_input(), embeddingGenerator=_embedding, searcher=searcher)

    assert len(searcher.calls) == 1
    args = searcher.calls[0]
    registryFilter = args[3]
    assert registryFilter.releaseId == 7
    assert registryFilter.domainCodes == ("PROCUREMENT",)
    assert registryFilter.entityTypes == ("SUPPLIER",)
    assert registryFilter.orgCodes == ("PROCUREMENT_ORG",)


async def test_ragRouteDegradesWhenSwitchIsOff(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关关 ⇒ fail-closed 降级，且**不发起向量调用**。"""
    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: False)
    searcher = _Recorder(RegistrySearchResult(hits=_hits(1), usedRegistryV2=True))

    async def _embedding(text: str) -> list[float]:
        raise AssertionError("开关关时不该生成查询向量")

    outcome = await runRagRoute(_input(), embeddingGenerator=_embedding, searcher=searcher)

    assert outcome.status == KNOWLEDGE_ROUTE_DEGRADED
    assert outcome.degraded_reason
    assert searcher.calls == []


async def test_ragRouteReportsGateDegradedReason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """闸门给了降级原因（v2 缺失等）⇒ 本路显式降级，**不静默空集**。"""
    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: True)
    searcher = _Recorder(
        RegistrySearchResult(hits=[], usedRegistryV2=False, degradedReason="v2_missing")
    )

    outcome = await runRagRoute(_input(), embeddingGenerator=_embedding, searcher=searcher)

    assert outcome.status == KNOWLEDGE_ROUTE_DEGRADED
    assert outcome.degraded_reason == "v2_missing"
    assert outcome.evidence == ()


async def test_ragRouteReturnsEmptyWhenNoHits(monkeypatch: pytest.MonkeyPatch) -> None:
    """没命中 ⇒ `EMPTY`（与降级分开）。"""
    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: True)
    searcher = _Recorder(RegistrySearchResult(hits=[], usedRegistryV2=True))

    outcome = await runRagRoute(_input(), embeddingGenerator=_embedding, searcher=searcher)

    assert outcome.status == KNOWLEDGE_ROUTE_EMPTY
    assert outcome.degraded_reason is None


async def test_ragRouteDegradesWhenReleaseUnavailable() -> None:
    """发布面不可用 ⇒ 显式降级（没有发布可过滤 ⇒ 不许放行）。"""

    async def _embedding(text: str) -> list[float]:
        raise AssertionError("发布面不可用时不该生成查询向量")

    outcome = await runRagRoute(
        _input(release=_degradedRelease()), embeddingGenerator=_embedding
    )

    assert outcome.status == KNOWLEDGE_ROUTE_DEGRADED
    assert outcome.degraded_reason


async def test_ragRouteDegradesWhenEmbeddingFails(monkeypatch: pytest.MonkeyPatch) -> None:
    """查询向量生成失败 ⇒ 降级，不许炸整轮查询。"""
    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: True)

    async def _failingEmbedding(text: str) -> list[float]:
        raise RuntimeError("embedding 服务不可用")

    outcome = await runRagRoute(_input(), embeddingGenerator=_failingEmbedding)

    assert outcome.status == KNOWLEDGE_ROUTE_DEGRADED
    assert outcome.degraded_reason


async def test_ragRouteRespectsTopK(monkeypatch: pytest.MonkeyPatch) -> None:
    """`topK` 真的裁到本路证据条数。"""
    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: True)
    searcher = _Recorder(RegistrySearchResult(hits=_hits(3), usedRegistryV2=True))

    outcome = await runRagRoute(
        _input(top_k=2), embeddingGenerator=_embedding, searcher=searcher
    )

    assert len(outcome.evidence) == 2


@pytest.mark.asyncio
async def test_ragEvidenceCarriesM21ContractFields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2-1 契约补全：证据带 evidenceId（全协唯一）+ confidence（距离→相似度）。

    - evidenceId 每条独立（default_factory）⇒ 调用方可逐条引用；
    - confidence = 1 - distance（夹到 [0,1]）——RAG 路唯一有分数的通道；
    - 旧字段（releaseId/releaseVersion/source）原样保留（旧调用方兼容）。
    """
    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: True)
    searcher = _Recorder(RegistrySearchResult(hits=_hits(3), usedRegistryV2=True))

    outcome = await runRagRoute(_input(), embeddingGenerator=_embedding, searcher=searcher)

    ids = [item.evidence_id for item in outcome.evidence]
    assert len(ids) == 3 and len(set(ids)) == 3, "evidenceId 必须逐条唯一"
    assert all(ids)
    confidences = [item.confidence for item in outcome.evidence]
    assert confidences == [0.9, 0.89, 0.88], (
        f"confidence = 1 - distance（distance 0.1/0.11/0.12），实际 {confidences}"
    )
    # 旧调用方兼容：既有字段原样
    assert outcome.evidence[0].release_id == 7
    assert outcome.evidence[0].source == KNOWLEDGE_SOURCE_RELEASE
    # camelCase 线格式
    dumped = outcome.evidence[0].model_dump(by_alias=True)
    assert "evidenceId" in dumped and "confidence" in dumped
