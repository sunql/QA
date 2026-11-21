"""P4 知识服务 Facade 的编排器（D32 骨架 → D33 图谱路 + 指标路 → D34 RAG/SQL 路）。

契约来源：`docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md` §6 P4
——「内部编排 图谱 / RAG / SQL / 指标 四路，统一引用与权限口径
（**一次查询内四路权限判定必须同源**）」。

编排层的三条纪律
----------------
1. **四路共用一个权限口径**：`permission.resolveKnowledgeScope()` 只推导**一次**，
   四路各自拿它**收窄**；任何一路都不得自己再推导一遍
   ⇒ 「同源」不是靠约定，是靠编排层把 scope 造出来再传下去。
2. **发布面只解一次**：钉住的发布快照在一次查询内**同一版**
   ⇒ 四路看到的口径一致，引用回溯到的版本也一致。
   解析器需要知道调用方是不是 admin（钉住发布的授权：非 admin 只能钉 ACTIVE）
   ⇒ `isAdmin` 由 scope 传下去（**只有这一处**能判定 admin）。
3. **每路都自带状态**：路由挂了 / 查不到 / 降级 / 无权查，必须落在
   `routeResults[].status` 与 `degradedReason` 上，**不许静默**。

⚠️ **本模块不调 LLM**（裁决 1）：产出的是「证据 + 确定性编排摘要」。
LLM 生成答案留在 chat / research / agent 既有通道（它们本来就读同一套发布钉住）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_GRAPH,
    KNOWLEDGE_CHANNEL_METRIC,
    KNOWLEDGE_CHANNEL_RAG,
    KNOWLEDGE_CHANNEL_SQL,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_DENIED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_ROUTE_PENDING,
    KnowledgeEntityRead,
    KnowledgeEvidenceRead,
    KnowledgeQueryRequest,
    KnowledgeQueryResponse,
    KnowledgeReleaseRef,
    KnowledgeRouteResultRead,
)
from app.services.knowledge.conflicts import detectConflicts
from app.services.knowledge.graph_route import runGraphRoute
from app.services.knowledge.messages import (
    MSG_CHANNEL_PENDING,
    MSG_DOMAIN_NOT_VISIBLE,
    MSG_ROUTE_FAILED,
    MSG_SCOPE_ENTITY_TYPES_EMPTY,
    MSG_SCOPE_SECURITY_LEVEL_IGNORED,
)
from app.services.knowledge.metric_route import runMetricRoute
from app.services.knowledge.permission import KnowledgeScope, resolveKnowledgeScope
from app.services.knowledge.rag_route import runRagRoute
from app.services.knowledge.release_context import (
    ResolvedRelease,
    resolveKnowledgeRelease,
)
from app.services.knowledge.route_input import RouteInput
from app.services.knowledge.route_outcome import RouteOutcome
from app.services.knowledge.sql_route import runSqlRoute

logger = logging.getLogger(__name__)

#: scope / 发布面的解析器签名（注入点 ⇒ 单测不需要 DB）。
ScopeResolver = Callable[..., Awaitable[KnowledgeScope]]
ReleaseResolver = Callable[..., Awaitable[ResolvedRelease]]
#: 路由函数签名（四路同形 ⇒ 编排层只有一个调用点）。
RouteRunner = Callable[[RouteInput], Awaitable[RouteOutcome]]

#: 通道的中文标签（`answerDraft` 用；顺序固定 ⇒ 输出可逐字比对）。
_CHANNEL_LABELS: dict[str, str] = {
    KNOWLEDGE_CHANNEL_GRAPH: "图谱",
    KNOWLEDGE_CHANNEL_METRIC: "指标",
    KNOWLEDGE_CHANNEL_RAG: "检索",
    KNOWLEDGE_CHANNEL_SQL: "SQL",
}

#: 通道 → 路由函数。**未接线的通道不在表里** ⇒ 走「未启用」占位。
_ROUTE_RUNNERS: dict[str, RouteRunner] = {
    KNOWLEDGE_CHANNEL_GRAPH: runGraphRoute,
    KNOWLEDGE_CHANNEL_METRIC: runMetricRoute,
    KNOWLEDGE_CHANNEL_RAG: runRagRoute,
    KNOWLEDGE_CHANNEL_SQL: runSqlRoute,
}


class KnowledgeFacade:
    """知识服务编排器。

    D32 只落**骨架**；D33 接入**图谱路 + 指标路**；D34 接入 **RAG 路 + SQL 路**；
    D35 收口「四路同一权限口径 + 引用回溯」的验收矩阵。
    """

    def __init__(
        self,
        *,
        scopeResolver: ScopeResolver | None = None,
        releaseResolver: ReleaseResolver | None = None,
        routeRunners: dict[str, RouteRunner] | None = None,
    ) -> None:
        """构造编排器。

        Args:
            scopeResolver: 权限口径解析器（默认真实现；单测注入假实现）。
            releaseResolver: 发布面解析器（默认真实现；单测注入假实现）。
            routeRunners: 路由表**按通道覆盖**（默认四路齐备；单测注入假实现）。
        """
        self._scopeResolver: ScopeResolver = scopeResolver or resolveKnowledgeScope
        self._releaseResolver: ReleaseResolver = releaseResolver or resolveKnowledgeRelease
        self._routeRunners: dict[str, RouteRunner] = {**_ROUTE_RUNNERS, **(routeRunners or {})}

    async def query(
        self,
        request: KnowledgeQueryRequest,
        *,
        user: CurrentUser,
        session: AsyncSession,
    ) -> KnowledgeQueryResponse:
        """编排一次知识查询。

        Args:
            request: 已通过契约校验的请求（通道枚举封闭、字段边界已定）。
            user: 当前调用方（权限口径的输入）。
            session: 数据库会话（取权限口径与发布面）。**必填**：权限推导与发布面
                解析都要读库 ⇒ 允许 `None` 只会把「忘了传会话」变成一个 500。

        Returns:
            `KnowledgeQueryResponse`：发布引用 + 编排摘要 + 实体 + 证据 + 每路状态。
        """
        scope = await self._scopeResolver(
            session,
            roles=user.roles or (),
            departments=user.departments or (),
            requestScope=request.scope,
        )
        channels = request.resolvedChannels()
        if request.domain not in scope.domain_codes:
            return self._deniedResponse(request, channels)

        release = await self._releaseResolver(
            session,
            domainCode=request.domain,
            releaseId=request.release_id,
            isAdmin=scope.is_admin,
        )
        entityFilter, entityWarnings = _entityFilter(request, scope)
        scopeWarnings = (*entityWarnings, *_ignoredScopeDimensions(request))
        routeInput = RouteInput(
            question=request.question,
            domain_code=request.domain,
            release=release,
            scope=scope,
            top_k=request.top_k,
            session=session,
            entity_filter=entityFilter,
        )
        outcomes: list[RouteOutcome] = []
        for channel in channels:
            outcomes.append(await self._runChannel(channel, routeInput))

        evidence: list[KnowledgeEvidenceRead] = []
        for outcome in outcomes:
            evidence.extend(outcome.evidence)
        entities: list[KnowledgeEntityRead] = []
        for outcome in outcomes:
            entities.extend(outcome.entities)

        routeResults = tuple(
            KnowledgeRouteResultRead(
                channel=outcome.channel,
                status=outcome.status,
                item_count=outcome.item_count,
                degraded_reason=outcome.degraded_reason,
                warnings=outcome.warnings,
            )
            for outcome in outcomes
        )
        # I2-B：跨通道证据冲突（§4.3——输出冲突提示与来源时间，不替模型选值）
        conflicts = detectConflicts(evidence)
        warnings = tuple(
            dict.fromkeys(
                [
                    *(conflict.reason for conflict in conflicts),
                    *scopeWarnings,
                    *(
                        reason
                        for outcome in outcomes
                        for reason in outcome.warnings
                    ),
                ]
            )
        )
        logger.info(
            "知识查询：domain=%s release=%s routes=%s",
            request.domain,
            release.version or "降级",
            ",".join(f"{row.channel}:{row.status}" for row in routeResults),
        )
        return KnowledgeQueryResponse(
            question=request.question,
            domain=request.domain,
            release=_releaseRef(release),
            answer_draft=_draft(routeResults, release),
            entities=tuple(entities),
            evidence=tuple(evidence),
            route_results=routeResults,
            warnings=warnings,
            conflicts=conflicts,
        )

    async def _runChannel(self, channel: str, inp: RouteInput) -> RouteOutcome:
        """跑一路：已接线的走真路由，未接线的走「未启用」占位。

        「四路均可单独返回」⇒ **一路抛异常不许带崩整轮查询** ⇒ 兜底降级
        （降级原因落到 `degradedReason`，调用方能分辨「这条路挂了」）。
        """
        runner = self._routeRunners.get(channel)
        if runner is None:
            return RouteOutcome(
                channel=channel,
                status=KNOWLEDGE_ROUTE_PENDING,
                warnings=(MSG_CHANNEL_PENDING,),
            )
        try:
            return await runner(inp)
        except Exception as exc:  # noqa: BLE001 —— 编排层兜底：单路失败不炸整轮
            logger.warning(
                "知识查询：%s 路异常，本路降级：%s", channel, exc, exc_info=True
            )
            return RouteOutcome(
                channel=channel,
                status=KNOWLEDGE_ROUTE_DEGRADED,
                degraded_reason=MSG_ROUTE_FAILED,
            )

    def _deniedResponse(
        self,
        request: KnowledgeQueryRequest,
        channels: tuple[str, ...],
    ) -> KnowledgeQueryResponse:
        """域不可见 ⇒ 空结果 + 警告（裁决 4：不是 403）。"""
        routeResults = tuple(
            KnowledgeRouteResultRead(
                channel=channel,
                status=KNOWLEDGE_ROUTE_DENIED,
                item_count=0,
                degraded_reason=None,
                warnings=(MSG_DOMAIN_NOT_VISIBLE,),
            )
            for channel in channels
        )
        logger.info("知识查询：域不可见（domain=%s）", request.domain)
        return KnowledgeQueryResponse(
            question=request.question,
            domain=request.domain,
            release=KnowledgeReleaseRef(degraded=True),
            answer_draft="",
            route_results=routeResults,
            warnings=(MSG_DOMAIN_NOT_VISIBLE,),
        )


def _entityFilter(
    request: KnowledgeQueryRequest, scope: KnowledgeScope
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    """请求侧对业务对象的收窄（**唯一入口**）：请求要的 ∩ 可见的。

    Returns:
        `(entityFilter, warnings)`：

        - `None` ⇒ 请求没要收窄 ⇒ 各路不收窄；
        - `()` ⇒ 要了但**交集为空**（要的那个业务对象不可见）⇒ 各路切片为空，
          并给一条警告（**不许**回落成「全都给」）；
        - 其余 ⇒ 收窄到这些业务对象。
    """
    requested = request.scope.entity_types if request.scope else ()
    if not requested:
        return None, ()
    allowed = set(scope.entity_types)
    effective = tuple(code for code in requested if code in allowed)
    if effective:
        return effective, ()
    return (), (MSG_SCOPE_ENTITY_TYPES_EMPTY,)


def _ignoredScopeDimensions(request: KnowledgeQueryRequest) -> tuple[str, ...]:
    """请求里传了、但**本批不消费**的维度 ⇒ 明确警告（不许「传了却当没传」）。"""
    if request.scope is None or request.scope.max_security_level is None:
        return ()
    return (MSG_SCOPE_SECURITY_LEVEL_IGNORED,)


def _releaseRef(release: ResolvedRelease) -> KnowledgeReleaseRef:
    """把内部发布面映射成对外的发布引用（降级时不挂版本号）。"""
    if release.degraded:
        return KnowledgeReleaseRef(degraded=True)
    return KnowledgeReleaseRef(
        id=release.release_id,
        version=release.version,
        manifest_hash=release.manifest_hash,
        degraded=False,
    )


def _statusText(status: str, itemCount: int) -> str:
    """状态 → 中文短语（确定性摘要用）。"""
    if status == KNOWLEDGE_ROUTE_OK:
        return f"命中 {itemCount} 条"
    if status == KNOWLEDGE_ROUTE_EMPTY:
        return "无命中"
    if status == KNOWLEDGE_ROUTE_DEGRADED:
        return "已降级"
    if status == KNOWLEDGE_ROUTE_DENIED:
        return "无权访问"
    if status == KNOWLEDGE_ROUTE_PENDING:
        return "未启用"
    return status


def _draft(routeResults: tuple[KnowledgeRouteResultRead, ...], release: ResolvedRelease) -> str:
    """确定性编排摘要（**不是** LLM 生成 ⇒ 同一输入同一输出）。"""
    parts = [
        f"{_CHANNEL_LABELS.get(row.channel, row.channel)}路{_statusText(row.status, row.item_count)}"
        for row in routeResults
    ]
    if release.degraded:
        return "；".join(parts)
    return f"口径版本 {release.version}；" + "；".join(parts)


__all__: list[str] = ["KnowledgeFacade", "ReleaseResolver", "ScopeResolver"]
