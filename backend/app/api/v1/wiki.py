"""Wiki 知识管理 API（feat-wiki-knowledge，Phase 8 M1）。

知识条目的增删改查 + 事实原子 / 关系读取：

- GET    /wiki/pages                 分页列表（可按 dimension/status 过滤）
- POST   /wiki/pages                 创建条目
- GET    /wiki/pages/{pageId}        条目详情
- PATCH  /wiki/pages/{pageId}        更新条目（含维度覆盖）
- POST   /wiki/pages/{pageId}/reclassify  机制 1：调整分类结论（写学习反馈）
- DELETE /wiki/pages/{pageId}        删除条目（级联清理 claim/relation）
- GET    /wiki/pages/{pageId}/claims     事实原子 + 证据
- GET    /wiki/pages/{pageId}/relations  知识关系（confirmedOnly 可选）
- POST   /wiki/pages/{pageId}/relations/discover  机制 2：发现关系候选
- POST   /wiki/relations/{relationId}/confirm     审核通过候选（写学习反馈）
- POST   /wiki/relations/{relationId}/reject      打回候选（保留打回痕迹）
- POST   /wiki/pages/{pageId}/conflicts/detect    机制 3：检测冲突
- GET    /wiki/pages/{pageId}/conflicts           某条目的冲突
- GET    /wiki/conflicts                          冲突看板列表
- POST   /wiki/conflicts/{conflictId}/resolve     处置冲突（写学习反馈）
- POST   /wiki/pages/{pageId}/suggestions         机制 4：产出结构化建议
- GET    /wiki/pages/{pageId}/suggestions         某条目的结构化建议
- GET    /wiki/suggestions                        建议工作台（跨条目，带标题）
- POST   /wiki/suggestions/{suggestionId}/accept  接受建议（写学习反馈 + 物化产物，终态）
- POST   /wiki/suggestions/{suggestionId}/reject  拒绝建议（写学习反馈，终态）
- POST   /wiki/pages/{pageId}/structure/recompute  机制 5：重算结构阶段（幂等）
- GET    /wiki/pages/{pageId}/rule            可执行规则（Agent 侧直读入口）
- GET    /wiki/pages/{pageId}/workflow        结构化流程
- POST   /wiki/pages/{pageId}/rules/dry-run   在样例上试跑规则（只读，不挂每路由限流）
- GET    /wiki/coverage               覆盖度矩阵（读快照）
- GET    /wiki/coverage/overview      机制 6：汇总 + 缺口清单 + 孤儿条目统计
- POST   /wiki/coverage/refresh       机制 6：全量重算覆盖度（幂等）
- GET    /wiki/coverage/domains       已用过的业务域词表
- GET    /wiki/coverage/mappings      本体类 → 业务域 标注列表
- POST   /wiki/coverage/mappings      标注业务域（幂等）
- DELETE /wiki/coverage/mappings      摘掉标注（query 参数，非 body）

**整组路由**都要求已认证（`dependencies=[Depends(getCurrentUser)]`，
依 `Harness/rules/权限与安全规范.md`「所有路由默认 Depends(getCurrentUser)」），
读接口登录即可、写接口另取当前用户做 created_by 溯源。

角色细分（KNOWLEDGE_OWNER/REVIEWER，谁能删谁能审）留到策展里程碑，
本轮**不做**：即任何已认证用户都能改删任意条目。这是刻意的阶段性简化，
不是遗漏——接 JWT/IdP 前必须补。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.wiki_coverage_models import GAP_UNLINKED
from app.domain.wiki_schemas import (
    ClassDomainMappingCreate,
    ClassDomainMappingRead,
    CoverageCellRead,
    CoverageDomainListRead,
    CoverageGapRead,
    CoverageOverviewRead,
    CoverageRefreshRead,
    CoverageSummaryRead,
    CoverageUnlinkedRead,
    KnowledgeClaimRead,
    KnowledgeConflictRead,
    KnowledgeRelationRead,
    ProcessWorkflowRead,
    StructureSuggestionRead,
    WikiConflictDetectRead,
    WikiConflictDetectRequest,
    WikiConflictListRead,
    WikiConflictResolveRequest,
    WikiPageCreate,
    WikiPageListRead,
    WikiPageRead,
    WikiPageUpdate,
    WikiReclassifyRead,
    WikiReclassifyRequest,
    WikiRelationDiscoverRead,
    WikiRelationDiscoverRequest,
    WikiRuleConditionRead,
    WikiRuleDryRunCaseRead,
    WikiRuleDryRunRead,
    WikiRuleDryRunRequest,
    WikiRuleDryRunSummaryRead,
    WikiRuleRead,
    WikiStructureStageRead,
    WikiSuggestionGenerateRead,
    WikiSuggestionGenerateRequest,
    WikiSuggestionListRead,
)
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.learning.conflict_detector import ConflictDetector
from app.services.learning.coverage_tracker import (
    DEFAULT_GAP_LIMIT,
    MAX_GAP_LIMIT,
    CoverageTracker,
)
from app.services.learning.llm_invoker import LearningLLMInvoker
from app.services.learning.relation_discovery import RelationDiscovery
from app.services.learning.structure_suggester import StructureSuggester
from app.services.wiki_conflict_service import WikiConflictService
from app.services.wiki_page_service import WikiPageService
from app.services.wiki_relation_service import WikiRelationService
from app.services.wiki_structure_service import WikiStructureService
from app.services.wiki_suggestion_service import WikiSuggestionService

router = APIRouter(
    prefix="/wiki",
    tags=["wiki"],
    # 整组路由默认要求认证：写接口曾漏挂 getCurrentUser，导致**未认证**的
    # PATCH/DELETE 可直接改删条目（code-review 发现）。router 级依赖一次
    # 覆盖全部路由，比逐路由挂更难漏。
    dependencies=[Depends(getCurrentUser)],
)
_wikiPageService = WikiPageService()
_relationDiscovery = RelationDiscovery()
_wikiRelationService = WikiRelationService()
_conflictDetector = ConflictDetector()
_wikiConflictService = WikiConflictService()
_structureSuggester = StructureSuggester()
_wikiSuggestionService = WikiSuggestionService()
_wikiStructureService = WikiStructureService()
_coverageTracker = CoverageTracker()


@router.get("/pages", response_model=WikiPageListRead, status_code=status.HTTP_200_OK)
async def listPages(
    dimension: str | None = Query(default=None, description="按知识维度过滤"),
    statusFilter: str | None = Query(default=None, alias="status", description="按状态过滤"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(getDb),
) -> WikiPageListRead:
    """分页列出知识条目。"""
    rows, total = await _wikiPageService.listPages(
        db, dimension=dimension, status=statusFilter, limit=limit, offset=offset
    )
    return WikiPageListRead(
        rows=[WikiPageRead.model_validate(r) for r in rows], total=total
    )


@router.post("/pages", response_model=WikiPageRead, status_code=status.HTTP_201_CREATED)
async def createPage(
    dto: WikiPageCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> WikiPageRead:
    """创建知识条目（created_by 取自当前用户，不信任客户端）。"""
    entity = await _wikiPageService.createPage(
        db, dto, createdByUserId=user.dbUserId
    )
    return WikiPageRead.model_validate(entity)


@router.get("/pages/{pageId}", response_model=WikiPageRead, status_code=status.HTTP_200_OK)
async def getPage(pageId: str, db: AsyncSession = Depends(getDb)) -> WikiPageRead:
    """按业务主键取条目详情。"""
    entity = await _wikiPageService.getPage(db, pageId)
    return WikiPageRead.model_validate(entity)


@router.patch("/pages/{pageId}", response_model=WikiPageRead, status_code=status.HTTP_200_OK)
async def updatePage(
    pageId: str,
    dto: WikiPageUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> WikiPageRead:
    """更新条目（PATCH 语义，未提供字段跳过）。

    提供 ``dimension`` 即视为对机制 1 建议的处置，actor 取自当前用户。
    """
    entity = await _wikiPageService.updatePage(
        db, pageId, dto, updatedByUserId=user.dbUserId
    )
    return WikiPageRead.model_validate(entity)


@router.post(
    "/pages/{pageId}/reclassify",
    response_model=WikiReclassifyRead,
    status_code=status.HTTP_200_OK,
)
async def reclassifyPage(
    pageId: str,
    dto: WikiReclassifyRequest,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> WikiReclassifyRead:
    """机制 1：业务专家调整分类结论（写回学习闭环反馈）。

    与 ``PATCH /pages/{pageId}`` 的区别只在**语义**：这里是「针对分类的
    显式处置」这一个动作的专用入口（UI 上是一个按钮，不是表单的一部分），
    落库路径完全共用 —— 见 ``WikiPageService.reclassify``。
    """
    entity, action = await _wikiPageService.reclassify(
        db, pageId, dimension=dto.dimension, updatedByUserId=user.dbUserId
    )
    return WikiReclassifyRead(page=WikiPageRead.model_validate(entity), action=action)


@router.delete("/pages/{pageId}", status_code=status.HTTP_204_NO_CONTENT)
async def deletePage(pageId: str, db: AsyncSession = Depends(getDb)) -> None:
    """删除条目（DB 级联清理 claim / evidence / relation）。"""
    await _wikiPageService.deletePage(db, pageId)


@router.get(
    "/pages/{pageId}/claims",
    response_model=list[KnowledgeClaimRead],
    status_code=status.HTTP_200_OK,
)
async def listClaims(
    pageId: str, db: AsyncSession = Depends(getDb)
) -> list[KnowledgeClaimRead]:
    """列出条目的事实原子（含证据出处）。"""
    entities = await _wikiPageService.listClaims(db, pageId)
    return [KnowledgeClaimRead.model_validate(e) for e in entities]


@router.get(
    "/pages/{pageId}/relations",
    response_model=list[KnowledgeRelationRead],
    status_code=status.HTTP_200_OK,
)
async def listRelations(
    pageId: str,
    confirmedOnly: bool = Query(default=False, description="只看已审核生效的关系"),
    db: AsyncSession = Depends(getDb),
) -> list[KnowledgeRelationRead]:
    """列出条目的传出知识关系。"""
    entities = await _wikiPageService.listRelations(db, pageId, confirmedOnly=confirmedOnly)
    return [KnowledgeRelationRead.model_validate(e) for e in entities]


@router.post(
    "/pages/{pageId}/relations/discover",
    response_model=WikiRelationDiscoverRead,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(rateLimitValue)
async def discoverRelations(
    request: Request,
    pageId: str,
    dto: WikiRelationDiscoverRequest,
    db: AsyncSession = Depends(getDb),
) -> WikiRelationDiscoverRead:
    """机制 2：为该条目发现关系候选（落库 ``confirmed=false``，等待人工审核）。

    ``modelId`` 给了才调用模型（LLM 实体抽取路径）；这一步**不校验模型可用性**
    前置失败，而是把失败记进 ``classExtractionStatus="FAILED"`` —— 确定性路径
    的结果不该因为模型没配好而一起丢掉。

    **限流**：与 ``/wiki/import/execute`` 同理，本接口是「用户输入 × LLM 调用」
    的放大入口——调用方既能选 ``modelId`` 又能决定正文内容，重复调用就是重复
    付费。全局默认限额（``SlowAPIMiddleware``，按 IP）已经兜底，这里显式声明
    一次是为了把「这是花钱的接口」写在代码上，而不是藏在全局配置里。
    """
    invoker = (
        LearningLLMInvoker(db, primaryModelId=dto.model_id)
        if dto.model_id is not None
        else None
    )
    result = await _relationDiscovery.discoverForPage(db, pageId, invoker=invoker)
    return WikiRelationDiscoverRead(
        candidates=[KnowledgeRelationRead.model_validate(e) for e in result.candidates],
        total=len(result.candidates),
        class_extraction_status=result.classExtractionStatus,
    )


@router.post(
    "/relations/{relationId}/confirm",
    response_model=KnowledgeRelationRead,
    status_code=status.HTTP_200_OK,
)
async def confirmRelation(
    relationId: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> KnowledgeRelationRead:
    """审核通过一条关系候选（写回学习闭环反馈）。"""
    entity = await _wikiRelationService.review(
        db, relationId, action="CONFIRM", userId=user.dbUserId
    )
    return KnowledgeRelationRead.model_validate(entity)


@router.post(
    "/relations/{relationId}/reject",
    response_model=KnowledgeRelationRead,
    status_code=status.HTTP_200_OK,
)
async def rejectRelation(
    relationId: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> KnowledgeRelationRead:
    """打回一条关系候选（保留行并盖章，避免下次发现又冒出来）。"""
    entity = await _wikiRelationService.review(
        db, relationId, action="REJECT", userId=user.dbUserId
    )
    return KnowledgeRelationRead.model_validate(entity)


# ---------------------------------------------------------------------------
# 机制 3：冲突检测与处置（M5）
# ---------------------------------------------------------------------------


@router.post(
    "/pages/{pageId}/conflicts/detect",
    response_model=WikiConflictDetectRead,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(rateLimitValue)
async def detectConflicts(
    request: Request,
    pageId: str,
    dto: WikiConflictDetectRequest,
    db: AsyncSession = Depends(getDb),
) -> WikiConflictDetectRead:
    """机制 3：为该条目检测冲突（落库待处置记录，返回**本次新增**的）。

    ``modelId`` 给了才跑 LLM 矛盾判定；确定性三路（失效引用 / 悬空引用 / 标题
    重复）始终跑，零 token。模型不可用不前置失败，而是记进 ``llmStatus="FAILED"``
    ——已经查实的三路不该因为模型没配好而一起丢掉。

    **限流**：与 ``discover`` 同理，这是「用户输入 × LLM 调用」的放大入口。
    """
    invoker = (
        LearningLLMInvoker(db, primaryModelId=dto.model_id)
        if dto.model_id is not None
        else None
    )
    result = await _conflictDetector.detectForPage(db, pageId, invoker=invoker)
    return WikiConflictDetectRead(
        conflicts=[
            KnowledgeConflictRead.model_validate(e) for e in result.conflicts
        ],
        total=len(result.conflicts),
        llm_status=result.llmStatus,
    )


@router.get(
    "/pages/{pageId}/conflicts",
    response_model=WikiConflictListRead,
    status_code=status.HTTP_200_OK,
)
async def listPageConflicts(
    pageId: str,
    db: AsyncSession = Depends(getDb),
) -> WikiConflictListRead:
    """列出与某条目相关的全部冲突。"""
    entities = await _wikiConflictService.listForPage(db, pageId)
    return WikiConflictListRead(
        rows=[KnowledgeConflictRead.model_validate(e) for e in entities],
        total=len(entities),
    )


@router.get(
    "/conflicts",
    response_model=WikiConflictListRead,
    status_code=status.HTTP_200_OK,
)
async def listConflicts(
    status_: str | None = Query(default=None, alias="status"),
    severity: str | None = Query(default=None),
    conflictType: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(getDb),
) -> WikiConflictListRead:
    """冲突看板的列表（可按 status / severity / conflictType 过滤）。

    ``status`` 参数在 Python 侧叫 ``status_``：模块顶部的
    ``from fastapi import status``（HTTP 状态码常量）会被这个形参遮蔽，
    而同文件其他路由都在用它。对外仍暴露为 ``status``（``alias``）。
    """
    entities, total = await _wikiConflictService.listConflicts(
        db,
        status=status_,
        severity=severity,
        conflictType=conflictType,
        limit=limit,
        offset=offset,
    )
    return WikiConflictListRead(
        rows=[KnowledgeConflictRead.model_validate(e) for e in entities],
        total=total,
    )


@router.post(
    "/conflicts/{conflictId}/resolve",
    response_model=KnowledgeConflictRead,
    status_code=status.HTTP_200_OK,
)
async def resolveConflict(
    conflictId: int,
    dto: WikiConflictResolveRequest,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> KnowledgeConflictRead:
    """处置一条冲突（RESOLVED / IGNORED / MERGED），并写一条学习反馈。"""
    entity = await _wikiConflictService.resolve(
        db, conflictId, action=dto.action, userId=user.dbUserId
    )
    return KnowledgeConflictRead.model_validate(entity)


# ---------------------------------------------------------------------------
# 机制 4：结构化建议（M5）
# ---------------------------------------------------------------------------


@router.post(
    "/pages/{pageId}/suggestions",
    response_model=WikiSuggestionGenerateRead,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(rateLimitValue)
async def generateSuggestions(
    request: Request,
    pageId: str,
    dto: WikiSuggestionGenerateRequest,
    db: AsyncSession = Depends(getDb),
) -> WikiSuggestionGenerateRead:
    """机制 4：为该条目产出结构化建议。

    ``modelId`` 给了才调模型。不给也**有用**：确定性预筛仍会跑，并在
    ``triggeredKind`` 里报出「这条知识像哪一类结构化知识」——用户据此决定要不要
    配个模型重跑，而不是对着空列表猜。

    已有待处置的同维度建议时直接跳过（不重复调用模型），见
    ``StructureSuggester.suggestForPage``。

    **限流**：与 ``discover`` / ``conflicts/detect`` 同理，这是「用户输入 × LLM
    调用」的放大入口。
    """
    invoker = (
        LearningLLMInvoker(db, primaryModelId=dto.model_id)
        if dto.model_id is not None
        else None
    )
    result = await _structureSuggester.suggestForPage(db, pageId, invoker=invoker)
    return WikiSuggestionGenerateRead(
        suggestions=[
            StructureSuggestionRead.model_validate(e) for e in result.suggestions
        ],
        total=len(result.suggestions),
        triggered_kind=result.triggeredKind,
        extraction_status=result.extractionStatus,
    )


@router.get(
    "/pages/{pageId}/suggestions",
    response_model=WikiSuggestionListRead,
    status_code=status.HTTP_200_OK,
)
async def listPageSuggestions(
    pageId: str,
    status_: str | None = Query(default=None, alias="status"),
    db: AsyncSession = Depends(getDb),
) -> WikiSuggestionListRead:
    """列出某条目的结构化建议（``status`` 可按 PENDING/ACCEPTED/REJECTED 过滤）。"""
    rows, total = await _wikiSuggestionService.listForPage(db, pageId, status=status_)
    return WikiSuggestionListRead(
        rows=[StructureSuggestionRead.model_validate(e) for e in rows], total=total
    )


@router.get(
    "/suggestions",
    response_model=WikiSuggestionListRead,
    status_code=status.HTTP_200_OK,
)
async def listAllSuggestions(
    status_: str | None = Query(
        default=None, alias="status", description="按 PENDING/ACCEPTED/REJECTED 过滤"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(getDb),
) -> WikiSuggestionListRead:
    """建议工作台：跨条目列出结构化建议（带条目标题）。

    与 ``/pages/{pageId}/suggestions`` 的区别是**视角**：那条是「这条知识有什么
    建议」，这条是「全库有哪些建议待处置」。审核人不该被迫逐个条目点进去找待办。
    """
    rows, total = await _wikiSuggestionService.listAll(
        db, status=status_, limit=limit, offset=offset
    )
    return WikiSuggestionListRead(
        rows=[StructureSuggestionRead.fromRow(e, title) for e, title in rows],
        total=total,
    )


@router.post(
    "/suggestions/{suggestionId}/accept",
    response_model=StructureSuggestionRead,
    status_code=status.HTTP_200_OK,
)
async def acceptSuggestion(
    suggestionId: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> StructureSuggestionRead:
    """接受一条结构化建议（写回学习闭环反馈）。

    **终态不可逆**：重复接受、或接受后再拒绝都返回 409 —— 接受会在 M6 物化出
    可执行规则/流程草稿，静默翻转状态会让建议与产物脱节。
    """
    entity = await _wikiSuggestionService.accept(db, suggestionId, userId=user.dbUserId)
    return StructureSuggestionRead.model_validate(entity)


@router.post(
    "/suggestions/{suggestionId}/reject",
    response_model=StructureSuggestionRead,
    status_code=status.HTTP_200_OK,
)
async def rejectSuggestion(
    suggestionId: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> StructureSuggestionRead:
    """拒绝一条结构化建议（保留行并盖终态，避免重复抽取又冒出来）。"""
    entity = await _wikiSuggestionService.reject(db, suggestionId, userId=user.dbUserId)
    return StructureSuggestionRead.model_validate(entity)


# ---------------------------------------------------------------------------
# 机制 5：渐进结构与规则 dry-run（M6）
# ---------------------------------------------------------------------------


@router.post(
    "/pages/{pageId}/structure/recompute",
    response_model=WikiStructureStageRead,
    status_code=status.HTTP_200_OK,
)
async def recomputeStructureStage(
    pageId: str, db: AsyncSession = Depends(getDb)
) -> WikiStructureStageRead:
    """重算结构阶段并落库（幂等）。

    阶段是**派生**的（见 ``progressive_upgrader``），所以这个接口不需要任何参数、
    也不会「越算越高」。它的用处是自愈：某次写入没落成功、或有人直接改了库，
    重新算一次就能回到与事实一致的状态。
    """
    change = await _wikiStructureService.recomputeStage(db, pageId)
    return WikiStructureStageRead(
        page_id=pageId,
        previous=change.previous,
        current=change.current,
        changed=change.changed,
    )


@router.get(
    "/pages/{pageId}/rule",
    response_model=WikiRuleRead,
    status_code=status.HTTP_200_OK,
)
async def getPageRule(
    pageId: str, db: AsyncSession = Depends(getDb)
) -> WikiRuleRead:
    """读条目的可执行规则（Agent 侧直读入口）。"""
    entity = await _wikiStructureService.getRule(db, pageId)
    return WikiRuleRead.model_validate(entity)


@router.get(
    "/pages/{pageId}/workflow",
    response_model=ProcessWorkflowRead,
    status_code=status.HTTP_200_OK,
)
async def getPageWorkflow(
    pageId: str, db: AsyncSession = Depends(getDb)
) -> ProcessWorkflowRead:
    """读条目的结构化流程。"""
    entity = await _wikiStructureService.getWorkflow(db, pageId)
    return ProcessWorkflowRead.model_validate(entity)


@router.post(
    "/pages/{pageId}/rules/dry-run",
    response_model=WikiRuleDryRunRead,
    status_code=status.HTTP_200_OK,
)
async def dryRunPageRule(
    pageId: str,
    dto: WikiRuleDryRunRequest | None = Body(default=None),
    db: AsyncSession = Depends(getDb),
) -> WikiRuleDryRunRead:
    """在样例上试跑规则，返回逐条比对结果。

    不传 ``examples`` 就用规则里存的样例。``examples`` 可选，故 **body 整体也可不传**
    —— 否则「不传 body」会先撞 422 请求校验，调用方拿不到「用存的样例」这条默认路径，
    文档里写的三态（不传 / 传空数组 / 传样例）在入口就少了一态。

    只读 + 纯函数，**不调模型、不写库**，
    故与 ``discover`` / ``suggestions`` 不同：这里**不加每路由限流** —— 重放不花钱，
    限流只会挡住正当的调试。注意全局默认限额（30 req/min/IP）仍然生效，它是这里
    真正的兜底；此外 ``examples`` 条数由 schema 限死，避免单请求内部放大。

    三类结果分计：``PASSED`` / ``FAILED`` / ``UNDECIDABLE``（样例缺字段导致判不了）
    —— 判不了不算失败，否则「样例没给全」会被读成「规则写错了」。
    """
    rule, report = await _wikiStructureService.dryRun(
        db, pageId, examples=dto.examples if dto is not None else None
    )
    return WikiRuleDryRunRead(
        rule_kind=rule.rule_kind,
        results=[
            WikiRuleDryRunCaseRead(
                index=case.index,
                input=case.input,
                expected_matched=case.expectedMatched,
                actual_matched=case.actualMatched,
                status=case.status,
                conditions=[
                    WikiRuleConditionRead(
                        field=c.field,
                        operator=c.operator,
                        expected=c.expected,
                        actual=c.actual,
                        present=c.present,
                        matched=c.matched,
                        undecidable_reason=c.undecidableReason,
                    )
                    for c in case.conditions
                ],
            )
            for case in report.results
        ],
        summary=WikiRuleDryRunSummaryRead(
            total=report.total,
            passed=report.passed,
            failed=report.failed,
            undecidable=report.undecidable,
        ),
    )


# ---------------------------------------------------------------------------
# 机制 6：覆盖度自感知（M7）
#
# 路由顺序：``/coverage`` 下没有 ``/{x}`` 形式的路由，故不存在
# 「静态段被参数路由吃掉」的陷阱（DQ 的 /rules/options 踩过）。仍按
# 静态 → 动态的习惯排列，便于以后加参数路由时不出事。
# ---------------------------------------------------------------------------


@router.get(
    "/coverage",
    response_model=list[CoverageCellRead],
    status_code=status.HTTP_200_OK,
)
async def listCoverageCells(
    domain: str | None = Query(default=None, description="按业务域过滤"),
    dimension: str | None = Query(default=None, description="按知识维度过滤"),
    coverageStatus: str | None = Query(
        default=None, alias="coverageStatus", description="按覆盖状态过滤"
    ),
    db: AsyncSession = Depends(getDb),
) -> list[CoverageCellRead]:
    """覆盖度矩阵（可按域/维度/状态过滤）。

    返回的是**上一次刷新**的快照，不是实时算的 —— 每次打开看板都全量重算会
    让一个只读页面变成写操作。要最新数据先 POST ``/coverage/refresh``。
    """
    rows = await _coverageTracker.listCells(
        db, domain=domain, dimension=dimension, coverageStatus=coverageStatus
    )
    return [CoverageCellRead.fromRow(cell, className) for cell, className in rows]


@router.get(
    "/coverage/overview",
    response_model=CoverageOverviewRead,
    status_code=status.HTTP_200_OK,
)
async def getCoverageOverview(
    domain: str | None = Query(default=None, description="按业务域过滤缺口"),
    includeUnassigned: bool = Query(
        default=False,
        alias="includeUnassigned",
        description="是否把「未标业务域」的格也算进缺口（默认否，见 service 说明）",
    ),
    gapLimit: int = Query(
        default=DEFAULT_GAP_LIMIT,
        alias="gapLimit",
        ge=0,
        le=MAX_GAP_LIMIT,
        description="缺口条数上限",
    ),
    db: AsyncSession = Depends(getDb),
) -> CoverageOverviewRead:
    """看板一次取齐：汇总 + 缺口清单 + 孤儿条目统计。

    三块数据是**同时**渲染的，合成一个响应避免前端并发三请求再拼装
    （也避免三份数据来自不同的刷新时刻）。
    """
    summary, gaps, unlinked = await _gatherOverview(
        db, domain=domain, includeUnassigned=includeUnassigned, gapLimit=gapLimit
    )
    return CoverageOverviewRead(
        summary=CoverageSummaryRead(**summary),
        gaps=[CoverageGapRead(**gap.__dict__) for gap in gaps],
        unlinked=CoverageUnlinkedRead(
            gap_type=GAP_UNLINKED,
            page_count=unlinked.pageCount,
            dimensions=unlinked.dimensions,
        ),
    )


@router.post(
    "/coverage/refresh",
    response_model=CoverageRefreshRead,
    status_code=status.HTTP_200_OK,
)
async def refreshCoverage(
    db: AsyncSession = Depends(getDb),
) -> CoverageRefreshRead:
    """全量重算覆盖度矩阵（幂等）。

    覆盖度是**派生快照**（见 ``coverage_tracker``），所以这个接口不需要参数、
    也不会「越刷越高」。它的用处是自愈：类被软删、域标注被摘掉后，重刷一次
    旧格就消失了。
    """
    result = await _coverageTracker.refresh(db)
    return CoverageRefreshRead(
        cell_count=result.cellCount,
        class_count=result.classCount,
        unmapped_class_count=result.unmappedClassCount,
        removed_count=result.removedCount,
        refreshed_at=result.refreshedAt,
    )


@router.get(
    "/coverage/domains",
    response_model=CoverageDomainListRead,
    status_code=status.HTTP_200_OK,
)
async def listCoverageDomains(
    db: AsyncSession = Depends(getDb),
) -> CoverageDomainListRead:
    """已使用过的业务域词表（从数据里去重取，不是写死的常量）。

    刻意不维护一份常量清单：域是**分类轴**，知识积累到新领域时就该冒出新域。
    """
    return CoverageDomainListRead(domains=await _coverageTracker.listDomains(db))


@router.get(
    "/coverage/mappings",
    response_model=list[ClassDomainMappingRead],
    status_code=status.HTTP_200_OK,
)
async def listDomainMappings(
    db: AsyncSession = Depends(getDb),
) -> list[ClassDomainMappingRead]:
    """列出全部「本体类 → 业务域」标注（供域标注界面对账）。"""
    rows = await _coverageTracker.listDomainMappings(db)
    return [
        ClassDomainMappingRead.fromRow(mapping, className)
        for mapping, className in rows
    ]


@router.post(
    "/coverage/mappings",
    response_model=ClassDomainMappingRead,
    status_code=status.HTTP_201_CREATED,
)
async def createDomainMapping(
    dto: ClassDomainMappingCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> ClassDomainMappingRead:
    """给本体类标注业务域（幂等：重复标注返回既有行，不报错）。

    ``domain`` **无白名单**：域是分类轴而非授权词表，锁死取值等于把
    「不锁业务域」这条核心诉求做废。只做 strip+upper 归一化 + 长度校验。
    """
    mapping = await _coverageTracker.assignDomain(
        db, dto.ontology_class_id, dto.domain, userId=user.dbUserId
    )
    className = await _classNameOf(db, mapping.ontology_class_id)
    return ClassDomainMappingRead.fromRow(mapping, className)


@router.delete(
    "/coverage/mappings",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def deleteDomainMapping(
    ontologyClassId: int = Query(..., alias="ontologyClassId", gt=0),
    domain: str = Query(..., min_length=1, max_length=100),
    db: AsyncSession = Depends(getDb),
) -> None:
    """摘掉一个域标注（没有这条标注则 404，不静默成功）。

    用 query 参数而非请求体：DELETE 带 body 在部分代理/客户端上会被丢掉，
    而这两个参数正是「要删哪一行」的全部信息，丢了就变成删错行或删不掉。
    """
    await _coverageTracker.removeDomain(db, ontologyClassId, domain)


async def _gatherOverview(
    db: AsyncSession,
    *,
    domain: str | None,
    includeUnassigned: bool,
    gapLimit: int,
) -> tuple[dict[str, Any], list[Any], Any]:
    """把看板要的三块数据一次取齐（顺序执行：同一个会话不能真并发）。"""
    summary = await _coverageTracker.summarize(db)
    gaps = await _coverageTracker.listGaps(
        db, domain=domain, includeUnassigned=includeUnassigned, limit=gapLimit
    )
    unlinked = await _coverageTracker.countUnlinkedPages(db)
    return summary, gaps, unlinked


async def _classNameOf(db: AsyncSession, ontologyClassId: int) -> str:
    """取类名（仅用于 DTO 展示；类不存在时给一个可读的占位）。"""
    from app.domain.models import OntologyClass

    name = (
        await db.execute(
            select(OntologyClass.class_name).where(
                OntologyClass.id == ontologyClassId
            )
        )
    ).scalar_one_or_none()
    return name or f"#{ontologyClassId}"
