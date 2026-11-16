"""P4 SQL 路（SQL）：从**发布快照**渲染命中候选类的**物理 schema**。

判据要点
--------
1. **物理层**：证据里能读到表名 / 列名 / 类型 / JOIN 键（不是语义层的类属性）。
2. **只渲染命中候选类**：不把整个域的 schema 塞进来（那是 NL2SQL prompt 的事）。
3. **按发布读**：证据带 `releaseId` + `releaseVersion` + `source=RELEASE`。
4. **空集正确返回**：没命中 ⇒ `EMPTY`（不是报错、不是降级）。
5. **发布面不可用 ⇒ 显式降级**：`DEGRADED` + 原因。
"""

from __future__ import annotations

from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_SQL,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_RELEASE,
)
from app.services.knowledge.permission import KnowledgeScope
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_input import RouteInput
from app.services.knowledge.sql_route import runSqlRoute
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


async def test_sqlRouteReturnsTableEvidenceTraceableToRelease() -> None:
    """证据带发布版本 ⇒ 引用可回溯；片段里能看到物理表 / 列 / 类型。"""
    outcome = await runSqlRoute(_input("供应商"))

    assert outcome.channel == KNOWLEDGE_CHANNEL_SQL
    assert outcome.status == KNOWLEDGE_ROUTE_OK
    assert outcome.evidence
    item = outcome.evidence[0]
    assert item.kind == "TABLE"
    assert item.ref == "Supplier"
    assert item.release_id == 7
    assert item.release_version == "2026.11.16-1"
    assert item.source == KNOWLEDGE_SOURCE_RELEASE
    assert item.snippet
    assert "T_SUPPLIER" in item.snippet
    assert "delivery_days" in item.snippet
    assert "varchar" in item.snippet


async def test_sqlRouteOnlyRendersMatchedCandidateClasses() -> None:
    """只渲染**命中**的候选类 ⇒ 片段里不出现别的类（域切片不等于候选集）。"""
    outcome = await runSqlRoute(_input("采购订单"))

    assert outcome.status == KNOWLEDGE_ROUTE_OK
    tableRefs = [item.ref for item in outcome.evidence if item.kind == "TABLE"]
    assert tableRefs == ["PurchaseOrder"]
    snippet = outcome.evidence[0].snippet or ""
    assert "T_PURCHASEORDER" in snippet
    assert "T_SUPPLIER" not in snippet


async def test_sqlRouteRendersJoinKeyBetweenMatchedClasses() -> None:
    """两个类都命中 ⇒ 它们的 JOIN 键也进证据（物理层的一部分）。"""
    outcome = await runSqlRoute(_input("采购订单 供应商"))

    joinItems = [item for item in outcome.evidence if item.kind == "JOIN"]
    assert joinItems
    assert joinItems[0].ref == "po_supplier"
    snippet = joinItems[0].snippet or ""
    assert "T_PURCHASEORDER" in snippet
    assert "T_SUPPLIER" in snippet


async def test_sqlRouteReturnsEmptyWhenNothingMatches() -> None:
    """没命中 ⇒ `EMPTY` + 0 条（不是报错、不是降级）。"""
    outcome = await runSqlRoute(_input("今天天气怎么样"))

    assert outcome.status == KNOWLEDGE_ROUTE_EMPTY
    assert outcome.evidence == ()
    assert outcome.degraded_reason is None


async def test_sqlRouteDegradesWhenReleaseUnavailable() -> None:
    """发布面不可用 ⇒ 显式降级，不许假装是发布读。"""
    outcome = await runSqlRoute(_input("供应商", release=_degradedRelease()))

    assert outcome.status == KNOWLEDGE_ROUTE_DEGRADED
    assert outcome.degraded_reason
    assert outcome.evidence == ()


async def test_sqlRouteRespectsTopK() -> None:
    """`topK` 真的裁到本路证据条数。"""
    outcome = await runSqlRoute(_input("采购订单 供应商", top_k=1))

    assert len(outcome.evidence) == 1
