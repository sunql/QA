"""P4 D35：四路**统一权限口径**的验收矩阵 + 引用回溯到发布版本。

矩阵的三个轴
------------
1. **身份**：可见域 / 不可见域（域不可见 ⇒ 四路一律 `DENIED`，**200 + 空结果 + 警告**，
   **不是** 403 —— 裁决 4）。
2. **请求侧收窄**：请求里的 scope **只能收窄**；收窄**只发生在权限源一处**
   （编排层把 `request.scope` 转交给 `resolveKnowledgeScope`）⇒ 各路**不许**自己再收窄一遍。
3. **引用可回溯**：每条 evidence 都带 `releaseId` + `releaseVersion` + `source=RELEASE`；
   发布面不可用 ⇒ 一律降级 + **不带版本号**（不许挂版本骗人）。

「同源」的硬证据
---------------
注入的假路由记录各自收到的 `RouteInput` ⇒ 断言**四路拿到的是同一个 scope 实例**
（`is` 身份比较，不只是「相等」）⇒ 「四路权限判定同源」不是靠约定，是靠**同一个对象**。

⚠️ 本文件在 `unit/` 下 ⇒ 允许新建（unit 分片按路径稳定哈希；integration 分片按排序下标
mod 4 轮转 ⇒ 新建 integration 文件会移动分片归属 ⇒ 假「净新增失败」，见 R-P4-1）。
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNELS,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_DENIED,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_RELEASE,
    KnowledgeQueryRequest,
    KnowledgeScopeRequest,
)
from app.services.knowledge.facade import KnowledgeFacade
from app.services.knowledge.messages import MSG_DOMAIN_NOT_VISIBLE
from app.services.knowledge.permission import KnowledgeScope
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_input import RouteInput
from app.services.knowledge.route_outcome import RouteOutcome
from app.tests._knowledge_manifest import sampleManifest

_USER = CurrentUser(userId="tester", roles=("user",), departments=("PROCUREMENT_ORG",))

#: 单测里**不碰库**的会话占位（权限与发布面都由假解析器提供 ⇒ session 只是签名要求）。
_FAKE_SESSION = cast(AsyncSession, object())


def _scope(domainCodes: tuple[str, ...] = ("PROCUREMENT",)) -> KnowledgeScope:
    return KnowledgeScope(
        domain_codes=domainCodes,
        org_codes=("PROCUREMENT_ORG",),
        entity_types=("SUPPLIER",),
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


async def _scopeResolver(session: Any, **kwargs: Any) -> KnowledgeScope:
    return _scope()


async def _releaseResolver(session: Any, **kwargs: Any) -> ResolvedRelease:
    return _release()


def _recordingFacade(
    *,
    scope: KnowledgeScope | None = None,
    release: ResolvedRelease | None = None,
) -> tuple[KnowledgeFacade, list[RouteInput]]:
    """注入**记录型**假路由（记录各自收到的 `RouteInput`）。"""
    inputs: list[RouteInput] = []

    def _makeRunner(channel: str):
        async def _runner(inp: RouteInput) -> RouteOutcome:
            inputs.append(inp)
            return RouteOutcome(channel=channel, status=KNOWLEDGE_ROUTE_OK)

        return _runner

    async def _scopeStub(session: Any, **kwargs: Any) -> KnowledgeScope:
        return scope if scope is not None else _scope()

    async def _releaseStub(session: Any, **kwargs: Any) -> ResolvedRelease:
        return release if release is not None else _release()

    facade = KnowledgeFacade(
        scopeResolver=_scopeStub,
        releaseResolver=_releaseStub,
        routeRunners={channel: _makeRunner(channel) for channel in KNOWLEDGE_CHANNELS},
    )
    return facade, inputs


async def test_allFourRoutesReceiveTheSameScopeInstance() -> None:
    """「四路权限判定同源」的硬证据：四路拿到的是**同一个** scope 对象。"""
    scope = _scope()
    release = _release()
    facade, inputs = _recordingFacade(scope=scope, release=release)
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert len(inputs) == len(KNOWLEDGE_CHANNELS)
    assert all(inp.scope is scope for inp in inputs)
    assert all(inp.release is release for inp in inputs)
    assert all(inp.domain_code == "PROCUREMENT" for inp in inputs)


async def test_requestScopeIsHandedToThePermissionSourceOnly() -> None:
    """请求侧收窄**只发生在权限源一处**（各路不许自己再收窄一遍）。"""
    seen: list[Any] = []

    async def _scopeSpy(session: Any, **kwargs: Any) -> KnowledgeScope:
        seen.append(kwargs.get("requestScope"))
        return _scope()

    facade = KnowledgeFacade(
        scopeResolver=_scopeSpy, releaseResolver=_releaseResolver
    )
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT",
        question="供应商",
        scope=KnowledgeScopeRequest(org_codes=("PROCUREMENT_ORG",)),
    )

    await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert len(seen) == 1
    assert seen[0] is request.scope


async def test_invisibleDomainDeniesEveryRoute() -> None:
    """域不可见 ⇒ 四路一律 `DENIED` + 空证据 + 警告（**不是** 403）。"""
    facade, inputs = _recordingFacade(scope=_scope(domainCodes=("QUALITY",)))
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert [row.channel for row in response.route_results] == list(KNOWLEDGE_CHANNELS)
    assert all(row.status == KNOWLEDGE_ROUTE_DENIED for row in response.route_results)
    assert all(row.item_count == 0 for row in response.route_results)
    assert response.evidence == ()
    assert MSG_DOMAIN_NOT_VISIBLE in response.warnings
    # 域不可见 ⇒ **一路都不该跑**（不许「先查了再判断能不能给」）。
    assert inputs == []


async def test_invisibleDomainNeverTouchesTheReleaseResolver() -> None:
    """域不可见 ⇒ **连发布面都不解**（不许用「版本号是否存在」侧漏域是否存在）。

    （刻画测试：编排层在解发布面**之前**就判域可见性。）
    """
    releaseCalls: list[Any] = []

    async def _releaseSpy(session: Any, **kwargs: Any) -> ResolvedRelease:
        releaseCalls.append(kwargs.get("domainCode"))
        return _release()

    async def _hiddenScopeResolver(session: Any, **kwargs: Any) -> KnowledgeScope:
        return _scope(domainCodes=("QUALITY",))

    facade = KnowledgeFacade(
        scopeResolver=_hiddenScopeResolver, releaseResolver=_releaseSpy
    )
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert releaseCalls == []
    assert response.release.degraded is True
    assert response.release.id is None
    assert response.release.version is None


async def test_degradedReleaseMakesEveryRouteDegradedAndStripsVersions() -> None:
    """发布面不可用 ⇒ 四路降级 + **不带版本号**（不许挂版本骗人）。"""

    async def _degradedResolver(session: Any, **kwargs: Any) -> ResolvedRelease:
        return _degradedRelease()

    facade = KnowledgeFacade(
        scopeResolver=_scopeResolver, releaseResolver=_degradedResolver
    )
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="供应商")

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert response.release.degraded is True
    assert response.release.id is None
    assert response.release.version is None
    assert response.evidence == ()
    assert all(row.status == KNOWLEDGE_ROUTE_DEGRADED for row in response.route_results)
    assert all(row.degraded_reason for row in response.route_results)


async def test_realRoutesStampReleaseVersionOnEveryEvidence() -> None:
    """**真路由**跑一遍：每条证据都带 `releaseId` / `releaseVersion` / `source=RELEASE`。"""
    facade = KnowledgeFacade(
        scopeResolver=_scopeResolver, releaseResolver=_releaseResolver
    )
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT", question="供应商 交付天数 采购订单金额"
    )

    response = await facade.query(request, user=_USER, session=_FAKE_SESSION)

    assert response.evidence
    for item in response.evidence:
        assert item.release_id == 7
        assert item.release_version == "2026.11.16-1"
        assert item.source == KNOWLEDGE_SOURCE_RELEASE
    okRows = [row for row in response.route_results if row.status == KNOWLEDGE_ROUTE_OK]
    assert {row.channel for row in okRows} == {"GRAPH", "METRIC", "SQL"}
