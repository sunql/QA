"""Wiki 知识管理 API（feat-wiki-knowledge，Phase 8 M1）。

知识条目的增删改查 + 事实原子 / 关系读取：

- GET    /wiki/pages                 分页列表（可按 dimension/status 过滤）
- GET    /wiki/pages/search          关键词检索（ILIKE 标题+正文，total=命中数）
- GET    /wiki/pages/semantic-search 语义检索（Milvus 向量相似度，feat-wiki-semantic-search）
- POST   /wiki/chat                  Wiki Chat 问答（SSE 流式，语义检索上下文 + LLM 合成，feat-wiki-chat）
- POST   /wiki/vector-sync           全量回填向量（admin-only，幂等对账自愈）
- POST   /wiki/pages                 创建条目
- GET    /wiki/pages/{pageId}        条目详情
- PATCH  /wiki/pages/{pageId}        更新条目（含维度覆盖）
- POST   /wiki/pages/{pageId}/reclassify  机制 1：调整分类结论（写学习反馈）
- DELETE /wiki/pages/{pageId}        删除条目（级联清理 claim/relation）
- POST   /wiki/pages/batch-delete    批量删除条目（部分成功 + 级联报告）
- GET    /wiki/pages/{pageId}/claims     事实原子 + 证据
- POST   /wiki/pages/{pageId}/claims/extract  机制 6：跑一次事实原子抽取（幂等）
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

import logging
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getAdminOnlyActor, getDb
from app.services.wiki_vector_service import WikiVectorError, WikiVectorService

logger = logging.getLogger(__name__)
from app.domain.wiki_coverage_models import GAP_UNLINKED
from app.domain.wiki_schemas import (
    ClaimExtractRead,
    ClaimExtractRequest,
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
    WikiChatRequest,
    WikiPageBatchDeleteCascadeRead,
    WikiPageBatchDeleteRead,
    WikiPageBatchDeleteRequest,
    WikiPageCreate,
    WikiPageListRead,
    WikiPageRead,
    WikiPageUpdate,
    WikiReclassifyRead,
    WikiReclassifyRequest,
    WikiClassifyPreviewRead,
    WikiRelationDiscoverRead,
    WikiRelationDiscoverRequest,
    WikiRelationSuggestCandidate,
    WikiRelationsSuggestRead,
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
from app.services.learning.auto_classifier import AutoClassifier
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


@router.get(
    "/pages/search",
    response_model=WikiPageListRead,
    status_code=status.HTTP_200_OK,
)
async def searchPages(
    query: str = Query(min_length=1, description="标题/正文检索词"),
    dimension: str | None = Query(default=None, description="按知识维度过滤"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(getDb),
) -> WikiPageListRead:
    """全文模糊检索（ILIKE 标题+正文）。

    独立端点而非给 listPages 加可选 query：两个 total 语义不同——
    listPages 的 total 是「全部条目数」（分页器用），这里的 total 是
    「命中数」（检索结果用）。该路由**必须**声明在 ``/pages/{pageId}``
    之前，否则会被路径参数吞掉。
    """
    if not query.strip():
        raise RequestValidationError(  # type: ignore[no-untyped-call]
            [{"loc": ["query", "query"], "msg": "must not be blank", "type": "value_error"}]
        )
    rows, total = await _wikiPageService.searchPages(
        db, query=query, dimension=dimension, limit=limit, offset=offset
    )
    return WikiPageListRead(
        rows=[WikiPageRead.model_validate(r) for r in rows], total=total
    )


@router.get("/pages/semantic-search", status_code=status.HTTP_200_OK)
async def semanticSearchPages(
    query: str = Query(min_length=1, description="自然语言查询（语义检索）"),
    dimension: str | None = Query(default=None, description="按知识维度过滤"),
    topK: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(getDb),
) -> list[dict]:
    """知识条目语义检索（Milvus 向量相似度，feat-wiki-semantic-search）。

    与 ``/pages/search``（ILIKE 关键词）互补：语义检索按含义匹配并带相似度
    得分排序。Milvus / embedding 基础设施故障映射 **503**（换部署问题），
    而非 422（换请求问题）。路由必须声明在 ``/pages/{pageId}`` 之前。
    """
    if not query.strip():
        raise RequestValidationError(  # type: ignore[no-untyped-call]
            [{"loc": ["query", "query"], "msg": "must not be blank", "type": "value_error"}]
        )
    try:
        return await WikiVectorService().searchSemantic(
            db, query, dimension=dimension, topK=topK
        )
    except WikiVectorError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"向量检索暂不可用（Milvus / embedding 故障）：{e}",
        ) from e


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


@router.post(
    "/pages/{pageId}/classify/preview",
    response_model=WikiClassifyPreviewRead,
    status_code=status.HTTP_200_OK,
)
async def classifyPagePreview(
    pageId: str,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> WikiClassifyPreviewRead:
    """Phase 5.5：MISSING_DIMENSION 缺口的两步预览。

    调 LLM 分类一次、**不写库** —— 把建议维度 + confidence + alternatives
    返给前端展示，用户在 Modal 里点确认后再走 ``PATCH /pages/{id}``。

    与 ``POST /reclassify`` 的关键差异：
    - /reclassify 是「用户已决定」的一次性写入 + 学习反馈
    - /classify/preview 是「让模型先说一句话」的预演，不动 page.dimension
    """
    from app.domain.exceptions import LLMUnavailableError
    from app.services.model_config_service import ModelConfigService

    page = await _wikiPageService.getPage(db, pageId)
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识条目不存在"
        )
    # 不传 model_id 就取第一个 active 配置 —— 与 /reclassify / detect 等
    # 都强制要求调用方显式选择不同，预览是「让模型说一句话」的兜底入口，
    # 默认配置即可，避免前端多一个必选字段。
    activeModels = await ModelConfigService().list(db, activeOnly=True)
    if not activeModels:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="系统未配置任何可用 LLM 模型",
        )
    invoker = LearningLLMInvoker(db, primaryModelId=activeModels[0].id)
    try:
        await invoker.preflight()
    except LLMUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    try:
        suggestion, _ = await AutoClassifier().classify(
            invoker, title=page.title, content=page.content
        )
    except LLMUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    if suggestion is None:
        # 模型返了非白名单维度或空内容 —— 前端展示「无可建议」
        return WikiClassifyPreviewRead(
            primary=None,
            confidence=0.0,
            alternatives=[],
            reason="模型未给出可信分类建议",
            rawOutput="",
        )
    return WikiClassifyPreviewRead(
        primary=suggestion.primary,
        confidence=suggestion.confidence,
        alternatives=list(suggestion.alternatives),
        reason=suggestion.reason,
        rawOutput=suggestion.toDict().get("rawOutput", ""),
    )


@router.delete("/pages/{pageId}", status_code=status.HTTP_204_NO_CONTENT)
async def deletePage(
    pageId: str,
    db: AsyncSession = Depends(getDb),
    user: CurrentUser = Depends(getCurrentUser),
) -> None:
    """删除条目（DB 级联清理 claim / evidence / relation）。

    审计记 ``DELETE`` + ``before`` 快照，actor 取自 ``X-User-Id``。路由级
    ``Depends(getCurrentUser)`` 已挡住未认证请求，这里再显式注入一次是为了
    **拿到 user 对象**（``use_cache=True``，不会重复解析 / 查库）。
    """
    await _wikiPageService.deletePage(db, pageId, user)


@router.post(
    "/pages/batch-delete",
    response_model=WikiPageBatchDeleteRead,
    status_code=status.HTTP_200_OK,
)
async def batchDeletePages(
    dto: WikiPageBatchDeleteRequest,
    db: AsyncSession = Depends(getDb),
    user: CurrentUser = Depends(getCurrentUser),
) -> WikiPageBatchDeleteRead:
    """批量删除知识条目（部分成功语义 + 级联报告）。

    与 ``DELETE /pages/{pageId}`` **同一套删除语义**（路由级认证、DB 级联、
    逐条写 DELETE 审计），区别只在「一批」与「一条」以及由此带来的边界行为：

    - 重复 id 去重（保序），不算错误；
    - 不存在的 id 进 ``notFound`` 而**不整体失败** —— 并发下别的用户先删了
      同一条很常见，回滚整批会让用户永远删不掉。响应显式回报，不是静默吞掉。

    **刻意不是 204**：批量操作必须能回答「哪几条没删掉」。

    路由顺序：``/pages/batch-delete`` 与 ``/pages/{pageId}`` 段数不同
    （前者 2 段、后者 1 段），且不存在 ``POST /pages/{pageId}``，
    故不会被动态段吃掉。仍紧邻单条删除排列，便于以后新增参数路由时对照。

    权限：沿用 router 级 ``Depends(getCurrentUser)``（见文件头说明）。
    本模块的角色细分（谁能删）仍留到策展里程碑，本轮不做 —— 批量入口
    **不新起一套**授权机制，否则会出现「单条删得掉、批量删不掉」这类
    两条路径两套规则的分叉。
    """
    result = await _wikiPageService.deletePages(db, dto.page_ids, user)
    return WikiPageBatchDeleteRead(
        requested=result.requested,
        deleted_page_ids=list(result.deletedPageIds),
        not_found=list(result.notFound),
        cascade=WikiPageBatchDeleteCascadeRead(
            claims=result.cascade.claims,
            relations=result.cascade.relations,
            suggestions=result.cascade.suggestions,
            rules=result.cascade.rules,
            workflows=result.cascade.workflows,
        ),
    )


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


@router.post(
    "/pages/{pageId}/claims/extract",
    response_model=ClaimExtractRead,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(rateLimitValue)
async def extractClaims(
    request: Request,
    pageId: str,
    dto: ClaimExtractRequest,
    db: AsyncSession = Depends(getDb),
) -> ClaimExtractRead:
    """机制 6：跑一次事实原子抽取。

    幂等：已抽过则返回 ``status=ALREADY_DONE, claim_count=0``，不重复写；
    ``force=True`` 跳过幂等保护重抽（换模型/内容修订场景）——先由 LLM 产出
    有效新结果才删旧 claims，LLM 失败/无效时旧数据原样保留。
    ``modelId`` 为空 = 跳过 LLM 直接返回 SKIPPED，与 detect / discover 同语义。
    不预检模型可用性 —— 调用失败由 ``ClaimExtractor.extractForPage`` 内部
    捕获并返回 FAILED，避免一次网络抖动让前端按钮永远转圈。

    **限流**：与 ``detect`` / ``discover`` 同档，这是「用户输入 × LLM 调用」
    的放大入口。
    """
    await _wikiPageService.getPage(db, pageId)  # 404 早失败
    statusValue, claimCount = await _wikiPageService.extractClaims(
        db, pageId, dto.model_id, force=dto.force
    )
    return ClaimExtractRead(status=statusValue, claim_count=claimCount)


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
        dropped_ghosts=list(result.droppedGhosts),
    )


@router.post(
    "/pages/{pageId}/relations/suggest",
    response_model=WikiRelationsSuggestRead,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(rateLimitValue)
async def suggestRelations(
    request: Request,
    pageId: str,
    dto: WikiRelationDiscoverRequest,
    db: AsyncSession = Depends(getDb),
) -> WikiRelationsSuggestRead:
    """Phase 5.5：ISOLATED_PAGE 缺口的两步预览。

    跑 ``discoverForPage(dryRun=True)`` 拿到「即将写入」的提案但**不落库**，
    把候选关系 + 幽灵引用列表返给前端 Modal；用户确认后再走现有的
    ``POST /pages/{pageId}/relations/discover`` 完成真正写入。

    与 ``/discover`` 的关键差异：
    - /discover 一次性 commit、可能重复打 token（连续点两次会各花一次）
    - /suggest 只读不写、即使连续点也不消耗额外 token 之外的副作用
      （会话关闭自动回滚未提交的提案 + 计量行）

    **限流**：与 ``/discover`` 同档（同样是「用户输入 × LLM 调用」的放大入口），
    这里的限流不是为了省钱（preview 路径已经走 LLM 了），而是防滥用 —— 用户
    短时间内反复点建议按钮会反复触发模型调用。
    """
    from sqlalchemy import select
    from app.domain.models import OntologyClass
    from app.domain.wiki_models import WikiPage

    invoker = (
        LearningLLMInvoker(db, primaryModelId=dto.model_id)
        if dto.model_id is not None
        else None
    )
    result = await _relationDiscovery.discoverForPage(
        db, pageId, invoker=invoker, dryRun=True
    )
    rawProposals = list(result.rawProposals)

    # 批量补上 target title：分两组查，PAGE 走 wiki_page，ONTOLOGY_CLASS 走 ontology_class
    pageIds = {
        p["downstream_id"] for p in rawProposals if p["downstream_type"] == "PAGE"
    }
    classIds = {
        p["downstream_id"]
        for p in rawProposals
        if p["downstream_type"] == "ONTOLOGY_CLASS"
    }
    titleByPageId: dict[str, str] = {}
    titleByClassId: dict[str, str] = {}
    if pageIds:
        rows = (
            await db.execute(
                select(WikiPage.page_id, WikiPage.title).where(
                    WikiPage.page_id.in_(pageIds)
                )
            )
        ).all()
        titleByPageId = {pid: title for pid, title in rows}
    if classIds:
        rows = (
            await db.execute(
                select(OntologyClass.id, OntologyClass.class_name).where(
                    OntologyClass.id.in_([int(x) for x in classIds if x.isdigit()])
                )
            )
        ).all()
        titleByClassId = {str(cid): name for cid, name in rows}

    def _reasonFor(proposal: dict) -> str:
        if proposal["relation_type"] == "REFERENCES":
            title = titleByPageId.get(proposal["downstream_id"], "")
            return f"正文出现条目《{title}》" if title else "正文引用了其它条目"
        if proposal["relation_type"] == "DESCRIBES":
            title = titleByClassId.get(proposal["downstream_id"], "")
            return f"正文出现本体类《{title}》" if title else "正文描述了某个本体类"
        return ""

    candidates: list[WikiRelationSuggestCandidate] = []
    for proposal in rawProposals:
        downstreamType = proposal["downstream_type"]
        downstreamId = proposal["downstream_id"]
        if downstreamType == "PAGE":
            title = titleByPageId.get(downstreamId, "")
        elif downstreamType == "ONTOLOGY_CLASS":
            title = titleByClassId.get(downstreamId, "")
        else:
            title = ""
        confidence = float(proposal.get("confidence") or 0.0)
        candidates.append(
            WikiRelationSuggestCandidate(
                downstream_type=downstreamType,
                downstream_id=downstreamId,
                downstream_title=title,
                relation_type=proposal["relation_type"],
                confidence=confidence,
                reason=_reasonFor(proposal),
            )
        )

    return WikiRelationsSuggestRead(
        candidates=candidates,
        total=len(candidates),
        class_extraction_status=result.classExtractionStatus,
        dropped_ghosts=list(result.droppedGhosts),
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


@router.post("/vector-sync", status_code=status.HTTP_200_OK)
async def vectorSync(
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    db: AsyncSession = Depends(getDb),
) -> dict[str, int]:
    """全量回填知识条目向量（admin-only 维护动作，feat-wiki-semantic-search）。

    幂等（upsert 语义）：写路径挂钩 best-effort 失败造成的漂移靠它对账自愈。
    条目多时耗时较长（逐条 embedding），前端应给进度提示或后台执行。
    """
    try:
        return await WikiVectorService().backfill(db)
    except WikiVectorError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"向量回填失败（Milvus / embedding 故障）：{e}",
        ) from e


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


# ---------------------------------------------------------------------------
# Wiki Chat（feat-wiki-chat）：语义知识库对话问答（SSE 流式）
# ---------------------------------------------------------------------------


@router.post("/chat")
@limiter.limit(rateLimitValue)
async def wikiChat(
    request: Request,
    dto: WikiChatRequest,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    """Wiki Chat SSE 流式问答。

    流程：ownership 守卫 → 加载 LlmConfigs → WikiQaService.answer_stream →
    每个 StreamEvent 走 toSse 序列化。
    """
    from collections.abc import AsyncIterator

    from app.domain.exceptions import DomainError
    from app.domain.models import SessionMessage
    from app.services.messages_zh import MSG_SESSION_NOT_OWNED
    from app.services.model_config_service import ModelConfigService
    from app.services.stream_events import EVENT_ERROR, StreamEvent
    from app.services.wiki_qa_service import WikiQaService

    # ownership 守卫：session 已有 wiki_qa 消息但 user_id 不匹配 → 422
    # （新 session 无行 → 放行；照 documents.py docQa 同模式）
    stmt = (
        select(SessionMessage.user_id)
        .where(
            SessionMessage.session_id == dto.session_id,
            SessionMessage.channel == "wiki_qa",
        )
        .limit(1)
    )
    existing_user_id = (await db.execute(stmt)).scalar()
    if existing_user_id is not None and existing_user_id != _user.userId:
        raise HTTPException(status_code=422, detail=MSG_SESSION_NOT_OWNED)

    configs = await ModelConfigService().list(db, activeOnly=True)
    svc = WikiQaService()

    async def eventSource() -> AsyncIterator[str]:
        try:
            async for event in svc.answer_stream(db, dto, actor=_user, configs=configs):
                yield event.toSse()
        except WikiVectorError as exc:
            # 向量链路（embedding/Milvus）不可用：LLM/依赖类错误
            yield StreamEvent(
                EVENT_ERROR, {"error": str(exc), "errorType": "LLM"},
            ).toSse()
        except DomainError as exc:
            yield StreamEvent(
                EVENT_ERROR,
                {"error": exc.message, "errorType": "DOMAIN", "detail": exc.detail},
            ).toSse()
        except Exception:  # noqa: BLE001 — SSE 已开始，兜底不能裸断连
            logger.exception("wiki chat 未预期异常 session=%s", dto.session_id)
            yield StreamEvent(
                EVENT_ERROR,
                {"error": "服务内部错误，请稍后重试", "errorType": "INTERNAL"},
            ).toSse()

    return StreamingResponse(
        eventSource(), media_type="text/event-stream",
    )
