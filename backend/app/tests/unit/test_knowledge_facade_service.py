"""P4 知识服务 Facade —— 编排层单测（D33：图谱路 + 指标路接入）。

钉住的是**编排层对外承诺**：

- 请求了哪几路 ⇒ `routeResults` 就有哪几路，**不多不少、顺序固定**
  （「四路均可单独返回」是 D34 的完成定义 ⇒ 编排层必须能按请求裁剪）；
- 每路都必须**自带状态**（`status` / `itemCount` / `degradedReason` / `warnings`）
  ⇒ 调用方能区分「查到了 0 条」「这条路挂了」「这个域我无权查」；
- **scope 只推导一次**（四路同源）⇒ 计数断言；
- 发布面不可用 ⇒ 显式降级标记，**不许挂版本号骗人**。

⚠️ 本文件在 `unit/` 下 ⇒ 允许新建（unit 分片按路径稳定哈希）。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNELS,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_DENIED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_ROUTE_PENDING,
    KnowledgeQueryRequest,
    KnowledgeScopeRequest,
)
from app.services.knowledge.facade import KnowledgeFacade
from app.services.knowledge.messages import (
    MSG_DOMAIN_NOT_VISIBLE,
    MSG_SCOPE_ENTITY_TYPES_EMPTY,
    MSG_SCOPE_SECURITY_LEVEL_IGNORED,
)
from app.services.knowledge.permission import KnowledgeScope
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_outcome import RouteOutcome
from app.tests._knowledge_manifest import sampleManifest

_USER = CurrentUser(userId="tester", roles=("user",), departments=("procurement",))

#: 单测里**不碰库**的会话占位（权限与发布面都由假解析器提供 ⇒ session 只是签名要求）。
_FAKE_SESSION = cast(AsyncSession, object())


def _scope(domainCodes: tuple[str, ...] = ("PROCUREMENT",)) -> KnowledgeScope:
    return KnowledgeScope(
        domain_codes=domainCodes,
        org_codes=("procurement",),
        entity_types=(),
        max_security_level="L1",
        is_admin=False,
    )


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


def _facade(
    *,
    scope: KnowledgeScope | None = None,
    release: ResolvedRelease | None = None,
    routeRunners: dict[str, Any] | None = None,
    releaseResolver: Any | None = None,
) -> tuple[KnowledgeFacade, list[int]]:
    """构造一个注入了假解析器的 facade（不发 DB 请求）。"""
    calls: list[int] = []

    async def _scopeResolver(session: Any, **kwargs: Any) -> KnowledgeScope:
        calls.append(1)
        return scope if scope is not None else _scope()

    async def _releaseResolver(session: Any, **kwargs: Any) -> ResolvedRelease:
        return release if release is not None else _release()

    return (
        KnowledgeFacade(
            scopeResolver=_scopeResolver,
            releaseResolver=releaseResolver or _releaseResolver,
            routeRunners=routeRunners,
        ),
        calls,
    )


def _adminScope() -> KnowledgeScope:
    """admin 的口径（admin ⇒ 可见全部 enabled 域，安全等级 L3）。"""
    return KnowledgeScope(
        domain_codes=("PROCUREMENT", "QUALITY"),
        org_codes=(),
        entity_types=(),
        max_security_level="L3",
        is_admin=True,
    )


async def test_releaseResolverReceivesAdminFlagFromScope() -> None:
    """发布面解析必须拿到 **admin 标记**（钉住发布的授权靠它判）。

    漏传 ⇒ 非 admin 也能钉住 DRAFT / 别域发布（越权开关）。
    """
    captured: dict[str, Any] = {}

    async def _releaseResolver(session: Any, **kwargs: Any) -> ResolvedRelease:
        captured.update(kwargs)
        return _release()

    async def _scopeResolver(session: Any, **kwargs: Any) -> KnowledgeScope:
        return _adminScope()

    facade = KnowledgeFacade(
        scopeResolver=_scopeResolver,
        releaseResolver=_releaseResolver,
        routeRunners={},
    )
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert captured["isAdmin"] is True
    assert captured["domainCode"] == "PROCUREMENT"


async def test_releaseResolverReceivesNonAdminFlagForRegularUser() -> None:
    """非 admin ⇒ `isAdmin=False` 传下去（不能因为「没传」而默认放行）。"""
    captured: dict[str, Any] = {}

    async def _releaseResolver(session: Any, **kwargs: Any) -> ResolvedRelease:
        captured.update(kwargs)
        return _release()

    facade, _calls = _facade(releaseResolver=_releaseResolver)
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert captured["isAdmin"] is False


async def test_graphRouteReturnsEvidenceWithReleaseVersion() -> None:
    """图谱路接入：证据带发布版本 ⇒ 引用可回溯。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT", question="交付延迟最高的供应商？", channels=("GRAPH",)
    )

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert response.route_results[0].channel == "GRAPH"
    assert response.route_results[0].status == KNOWLEDGE_ROUTE_OK
    assert response.route_results[0].item_count > 0
    assert response.evidence
    for item in response.evidence:
        assert item.release_id == 7
        assert item.release_version == "2026.11.16-1"


async def test_metricRouteReturnsEvidence() -> None:
    """指标路接入：指标 / KPI 命中并带发布版本。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT", question="供应商交付天数", channels=("METRIC",)
    )

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert response.route_results[0].channel == "METRIC"
    assert response.route_results[0].status == KNOWLEDGE_ROUTE_OK
    assert response.route_results[0].item_count > 0


async def test_allFourChannelsAreWiredInD34() -> None:
    """D34：四路**全部接线**（不再有「未启用」占位）。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    statuses = {row.channel: row.status for row in response.route_results}
    assert statuses["GRAPH"] == KNOWLEDGE_ROUTE_OK
    assert statuses["SQL"] == KNOWLEDGE_ROUTE_OK
    assert statuses["RAG"] != KNOWLEDGE_ROUTE_PENDING
    assert statuses["METRIC"] != KNOWLEDGE_ROUTE_PENDING


async def test_sqlRouteReturnsPhysicalSchemaEvidence() -> None:
    """SQL 路单独可返回：证据里能看到物理表 / 列 / 类型。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT", question="供应商", channels=("SQL",)
    )

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    row = response.route_results[0]
    assert row.channel == "SQL"
    assert row.status == KNOWLEDGE_ROUTE_OK
    assert response.evidence
    assert "T_SUPPLIER" in (response.evidence[0].snippet or "")
    for item in response.evidence:
        assert item.release_id == 7
        assert item.source == "RELEASE"


async def test_ragRouteDegradesWhenCapabilityDisabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAG 路单独可返回：能力未启用 ⇒ 显式降级（fail-closed），不是静默空集。"""
    from app.services.knowledge import rag_route

    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: False)
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT", question="供应商", channels=("RAG",)
    )

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    row = response.route_results[0]
    assert row.channel == "RAG"
    assert row.status == KNOWLEDGE_ROUTE_DEGRADED
    assert row.degraded_reason


async def test_oneFailingRouteDoesNotBreakWholeQuery() -> None:
    """「四路均可单独返回」⇒ 一路抛异常不许带崩整轮查询（该路降级，其余照常）。"""

    async def _boom(inp: Any) -> RouteOutcome:
        raise RuntimeError("路由炸了")

    facade, _calls = _facade(
        routeRunners={"RAG": _boom},
    )
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    statuses = {row.channel: row.status for row in response.route_results}
    assert statuses["RAG"] == KNOWLEDGE_ROUTE_DEGRADED
    assert statuses["GRAPH"] == KNOWLEDGE_ROUTE_OK
    assert statuses["SQL"] == KNOWLEDGE_ROUTE_OK


async def test_scopeIsResolvedOnlyOncePerQuery() -> None:
    """四路同源的硬证据：一次查询只推导一次 scope。"""
    facade, calls = _facade()
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert len(calls) == 1


async def test_invisibleDomainReturnsEmptyWithWarning() -> None:
    """域不可见 ⇒ 空结果 + 警告（**不是 403**，裁决 4）。"""
    facade, _calls = _facade(scope=_scope(domainCodes=("QUALITY",)))
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert response.evidence == ()
    assert response.release.degraded is True
    assert response.release.id is None
    assert MSG_DOMAIN_NOT_VISIBLE in response.warnings
    for row in response.route_results:
        assert row.status == KNOWLEDGE_ROUTE_DENIED
        assert row.item_count == 0
        assert MSG_DOMAIN_NOT_VISIBLE in row.warnings


async def test_degradedReleaseMarksRoutesDegraded() -> None:
    """发布面不可用 ⇒ 显式降级标记，不许假装是发布读。"""
    facade, _calls = _facade(release=_degradedRelease())
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert response.release.degraded is True
    assert response.release.version is None
    assert response.evidence == ()
    graphRow = next(row for row in response.route_results if row.channel == "GRAPH")
    assert graphRow.status == KNOWLEDGE_ROUTE_DEGRADED
    assert graphRow.degraded_reason


async def test_emptyMatchReturnsEmptyNotDegraded() -> None:
    """没命中 ⇒ EMPTY（不是降级、不是报错）。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="今天天气怎么样")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    graphRow = next(row for row in response.route_results if row.channel == "GRAPH")
    assert graphRow.status == KNOWLEDGE_ROUTE_EMPTY
    assert graphRow.degraded_reason is None


async def test_requestedChannelsAreTheOnlyOnesReturned() -> None:
    """只请求一路 ⇒ 只跑那一路。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT", question="供应商", channels=("METRIC",)
    )

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert [row.channel for row in response.route_results] == ["METRIC"]


async def test_defaultsToAllFourChannelsInFixedOrder() -> None:
    """缺省 = 四路全开（顺序固定 ⇒ 输出可逐字比对）。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert [row.channel for row in response.route_results] == list(KNOWLEDGE_CHANNELS)


async def test_answerDraftIsDeterministicSummary() -> None:
    """`answerDraft` 是确定性编排摘要（不是 LLM 生成 ⇒ 同一输入同一输出）。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    first = await facade.query(request, user=_USER, session=_FAKE_SESSION)
    second = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert first.answer_draft
    assert first.answer_draft == second.answer_draft


async def test_topKIsForwardedToRoutes() -> None:
    """`topK` 必须真的裁到各路（否则召回条数不受控）。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT", question="采购订单 供应商 交付天数", top_k=1
    )

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    graphRow = next(row for row in response.route_results if row.channel == "GRAPH")
    assert graphRow.item_count == 1


async def test_echoesQuestionAndDomain() -> None:
    """请求原样回显（域已去空白）。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(domain="  PROCUREMENT  ", question="交付延迟？")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert response.domain == "PROCUREMENT"
    assert response.question == "交付延迟？"


# --- 请求侧收窄真的生效（scope 不是摆设）--------------------------------------


def _scopeWithEntityTypes(entityTypes: tuple[str, ...]) -> KnowledgeScope:
    """可见业务对象集合 = `entityTypes`（模拟「这些业务对象对用户可见」）。"""
    return KnowledgeScope(
        domain_codes=("PROCUREMENT",),
        org_codes=("procurement",),
        entity_types=entityTypes,
        max_security_level="L1",
        is_admin=False,
    )


async def test_requestedEntityTypesActuallyNarrowTheSlice() -> None:
    """请求侧 `entityTypes` **必须真的收窄**切片（否则 scope 只是个摆设）。

    对照组：不收窄时「采购订单金额」命中 `po_amount`；收窄到 `SUPPLIER` 后
    `PurchaseOrder` 不在切片里 ⇒ 指标路变 EMPTY。
    """
    scope = _scopeWithEntityTypes(("SUPPLIER", "PURCHASE_ORDER"))
    facade, _calls = _facade(scope=scope)
    question = "采购订单金额"

    plain = await facade.query(
        KnowledgeQueryRequest(domain="PROCUREMENT", question=question, channels=("METRIC",)),
        user=_USER,
        session=_FAKE_SESSION,
    )
    narrowed = await facade.query(
        KnowledgeQueryRequest(
            domain="PROCUREMENT",
            question=question,
            channels=("METRIC",),
            scope=KnowledgeScopeRequest(entity_types=("SUPPLIER",)),
        ),
        user=_USER,
        session=_FAKE_SESSION,
    )

    assert plain.route_results[0].status == KNOWLEDGE_ROUTE_OK
    assert narrowed.route_results[0].status == KNOWLEDGE_ROUTE_EMPTY


async def test_requestedEntityTypesAreForwardedToRoutes() -> None:
    """收窄结果**只有一个入口**（编排层算好）⇒ 四路拿到同一个 `entity_filter`。"""
    captured: list[Any] = []

    async def _capture(inp: Any) -> RouteOutcome:
        captured.append(inp)
        return RouteOutcome(channel="GRAPH", status=KNOWLEDGE_ROUTE_EMPTY)

    scope = _scopeWithEntityTypes(("SUPPLIER", "PURCHASE_ORDER"))
    facade, _calls = _facade(scope=scope, routeRunners={"GRAPH": _capture})
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT",
        question="供应商",
        channels=("GRAPH",),
        scope=KnowledgeScopeRequest(entity_types=("SUPPLIER",)),
    )

    await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert captured[0].entity_filter == ("SUPPLIER",)


async def test_requestedEntityTypesOutsideVisibleScopeYieldEmptyResult() -> None:
    """要的业务对象**不可见** ⇒ 空结果 + 明确警告（不许回落成「全都给」）。"""
    scope = _scopeWithEntityTypes(("SUPPLIER",))
    facade, _calls = _facade(scope=scope)
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT",
        question="供应商",
        channels=("GRAPH",),
        scope=KnowledgeScopeRequest(entity_types=("NOT_VISIBLE_BO",)),
    )

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert response.route_results[0].status == KNOWLEDGE_ROUTE_EMPTY
    assert response.evidence == ()
    assert MSG_SCOPE_ENTITY_TYPES_EMPTY in response.warnings


async def test_maxSecurityLevelIsReportedAsIgnored() -> None:
    """显式传了 `maxSecurityLevel` ⇒ 必须告知「本批不生效」（不许静默忽略）。"""
    facade, _calls = _facade()
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT",
        question="供应商",
        scope=KnowledgeScopeRequest(max_security_level="L2"),
    )

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert MSG_SCOPE_SECURITY_LEVEL_IGNORED in response.warnings


# ---------------------------------------------------------------------------
# I2-B：跨通道证据冲突（§4.3）——不替模型选值
# ---------------------------------------------------------------------------


def _conflictRunners() -> dict[str, Any]:
    """GRAPH 与 SQL 两路对同一类给出不同内容 ⇒ 必出 conflict。"""
    from app.domain.knowledge_schemas import (
        KNOWLEDGE_CHANNEL_GRAPH,
        KNOWLEDGE_CHANNEL_SQL,
        KnowledgeEvidenceRead,
    )

    async def _graph(inp: Any) -> RouteOutcome:
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_GRAPH,
            status=KNOWLEDGE_ROUTE_OK,
            evidence=(
                KnowledgeEvidenceRead(
                    channel=KNOWLEDGE_CHANNEL_GRAPH, kind="CLASS",
                    ref="DIM_SUPPLIER", title="供应商主数据",
                    snippet="248 个属性", release_version="v1",
                ),
            ),
        )

    async def _sql(inp: Any) -> RouteOutcome:
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_SQL,
            status=KNOWLEDGE_ROUTE_OK,
            evidence=(
                KnowledgeEvidenceRead(
                    channel=KNOWLEDGE_CHANNEL_SQL, kind="CLASS",
                    ref="DIM_SUPPLIER", title="供应商主数据",
                    snippet="240 列", release_version="v1",
                ),
            ),
        )

    return {KNOWLEDGE_CHANNEL_GRAPH: _graph, KNOWLEDGE_CHANNEL_SQL: _sql}


@pytest.mark.asyncio
async def test_facadeReportsCrossChannelConflicts() -> None:
    """两路内容不一致 ⇒ 响应带 conflicts + warning（不替模型选值）。"""
    facade, _calls = _facade(routeRunners=_conflictRunners())
    response = await facade.query(
        KnowledgeQueryRequest(
            domain="PROCUREMENT",
            question="供应商主数据",
            channels=("GRAPH", "SQL"),
            top_k=5,
        ),
        user=_USER,
        session=_FAKE_SESSION,
    )

    assert len(response.conflicts) == 1
    conflict = response.conflicts[0]
    assert conflict.key == "CLASS:DIM_SUPPLIER"
    assert conflict.channels == ("GRAPH", "SQL")
    assert any("不同内容" in w for w in response.warnings), "冲突必须进 warnings"
    dumped = conflict.model_dump(by_alias=True)
    assert dumped["channels"] == ("GRAPH", "SQL")
    assert dumped["entries"][0]["channel"] == "GRAPH"
