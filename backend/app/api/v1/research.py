"""研究型入口 REST API（feat-research-entry Task 7）。

端点（前缀 `/api/v1/research`）：
- POST   /sessions                          新建会话（201）
- GET    /sessions                          当前用户的会话列表
- GET    /sessions/{sessionId}              {session, turns, pendingCheckpoint}
- DELETE /sessions/{sessionId}              硬删会话（DB CASCADE 清子表，归属不符 404）
- POST   /sessions/{sessionId}/turns        202 + 状态机后台跑（进度走 SSE）
- POST   /checkpoints/{checkpointId}/answer {sessionStatus, nextPhase}
- GET    /sessions/{sessionId}/report       ?version=N → 指定版 / 当前 published
- GET    /sessions/{sessionId}/reports      版本列表
- GET    /stream?sessionId=...              SSE：research.* 事件族（Task 8，独立端点）

SSE 通道（Task 8）：状态机的事件经 `emit` 接线**先落** `research_event_outbox`（Task 12，
持久化台账 + 单调 `seq` 游标）**再**广播到进程内 `ResearchEventBus`（见
`_runTurnInBackground` / `_resumeTurnInBackground` 的 `functools.partial(_persistAndPublish, ...)`），
流端**先订阅再回放**（`?lastEventId=`）并返回 `StreamingResponse`。与 chat 的 SSE 实现
**完全独立**（设计 §4.5）。

鉴权与越权（安全）：
- router 级 `Depends(getCurrentUser)`（read-only 也强制，见 memory: router auth 强制）；
- 研究域**要求可归属身份**：stub 匿名（dbUserId=None）无 owner 语义 → 401，
  否则 created_by=NULL 的会话会互相「认领」（`_requireResearchUser`）；
- 所有 session 读写（含子资源）校验 `created_by == user.dbUserId`，不匹配一律
  **404**（与 wiki_import 同款横向隔离语义，不泄露存在性）。

事务模型（Task 5 LOW-9 对账）：
- 请求内只写「立刻要返回的东西」（新建会话 / user turn / 决策前的只读校验），
  由 `getDb` 的会话提交；状态机**不在请求事务里跑**；
- 状态机跑在 `BackgroundTasks` 里（Starlette 语义：响应先发出、任务随后执行；
  测试 ASGITransport 下随 ASGI 调用一并完成 ⇒ 确定性），并**自开会话**——
  请求会话在响应发出后即关闭，复用会撞 MissingGreenlet / 已关闭连接；
- 状态机自身在 execute / verify 相位边界 `commit`（Task 5 设计），这就是
  暂停点与 finding 的持久化保证；后台包装只做收尾 commit / 兜底 rollback。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from collections.abc import AsyncIterator
from functools import partial
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import (
    AuthFailedError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.domain.models import DataSource, OntologyClass
from app.domain.research_models import (
    ResearchCheckpoint,
    ResearchReport,
    ResearchSession,
    ResearchTurn,
)
from app.domain.research_schemas import (
    CheckpointAnswerRead,
    CheckpointAnswerRequest,
    ResearchCheckpointRead,
    ResearchReportRead,
    ResearchReportSummaryRead,
    ResearchSessionAbortRead,
    ResearchSessionCreate,
    ResearchSessionDetail,
    ResearchSessionRead,
    ResearchTurnAccepted,
    ResearchTurnCreate,
    ResearchTurnRead,
)
from app.infrastructure.database import getSessionFactory
from app.infrastructure.llm.factory import createClient
from app.services.chart_service import ChartService
from app.services.datasource_service import DataSourceService
from app.services.enterprise_semantic_layer import EnterpriseSemanticLayer
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_semantic_match_service import KpiSemanticMatchService
from app.services.messages_zh import MSG_DATASOURCE_NONE_AVAILABLE, MSG_MODEL_CONFIG_UNAVAILABLE
from app.services.model_config_service import ModelConfigService
from app.services.nl2sql_service import Nl2SqlService
from app.services.ontology_service import OntologyService
from app.services.research_agent_phases import decideResume
from app.services.research_agent_ports import (
    ACTION_STATUS,
    ERROR_CHECKPOINT_CONFLICT,
    ERROR_TURN_FAILED,
    EVENT_DONE,
    EVENT_ERROR,
    STATUS_RUNNING,
    TERMINAL_ERROR_CODES,
    errorPayload,
)
from app.services.research_agent_service import ResearchAgentService
from app.services.research_event_bus import EVENT_CONNECTED, bus
from app.services.research_event_outbox import (
    _REPLAY_LIMIT,
    fetchEventsAfter,
    persistEvent,
)
from app.services.research_json_encoder import research_json
from app.services.research_session_service import (
    PROMPT_KEY,
    TERMINAL_SESSION_STATUSES,
    CheckpointConflictError,
    CheckpointStaleError,
    ResearchSessionService,
    RunClaimConflictError,
    _coerceJsonSafe,
)
from app.services.research_sql_runner import ResearchSqlRunner
from app.services.session_claim import (
    _MSG_RESEARCH_SESSION_NOT_FOUND,
    assert_research_claim_invariant,
    assert_research_session_ownership,
)
from app.services.research_task_service import (    TASK_KIND_RESUME,
    TASK_KIND_TURN,
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    claimTask,
    enqueueTask,
    finishTask,
)
from app.services.step_query_planner import StepQueryPlanner
from app.services.token_usage_service import TokenUsageService
from app.services.wiki_vector_service import WikiVectorService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research", dependencies=[Depends(getCurrentUser)])

_sessionService = ResearchSessionService()
"""无状态服务单例（所有方法首参为 session，无跨请求状态）。"""

_ESL_TOP_K = 5
"""三臂检索条数（与 `EnterpriseSemanticLayer._TOP_K` 同值；ESL 调 searcher 时显式传 topK）。"""

_bus = bus
"""进程内事件总线单例（模块级别名：便于测试与诊断，见 research_event_bus）。"""

_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
"""台账认领者标识（Task 10+11）：进程启动时算一次（host:pid）—— 崩溃后新进程 id 不同，
便于在 ``research_task.worker_id`` 上区分「谁认领的」。"""


async def _persistAndPublish(sessionId: str, event: str, payload: dict[str, Any]) -> None:
    """先写 outbox（独立短会话、自记账、不抛），再广播（带 ``seq``）。推流失败不改变 turn 结果。

    **为什么用独立短会话**（`getSessionFactory()()`，不用状态机的 session）：状态机在相位
    边界 commit、异常时整事务 rollback；若 outbox 行挂在**同一**事务上，回滚会连同**已广播
    给用户的事件**一起从台账里消失，重连回放就出现空洞。独立会话让台账与业务事务解耦。

    写失败**不抛**（与 `emitEvent` 同口径）：`seq=None` ⇒ 帧里不写 `id:`，客户端拿不到该
    事件的游标，但 turn 照常推进。本函数可作 `emit` 的 partial 绑定面。
    """
    seq: int | None = None
    try:
        async with getSessionFactory()() as session:
            seq = await persistEvent(
                session, sessionId=uuid.UUID(sessionId), event=event, payload=payload
            )
            await session.commit()
    except Exception:  # noqa: BLE001 —— 台账写失败不得杀 turn
        logger.exception("研究事件 outbox 落库事务失败: session=%s event=%s", sessionId, event)
    await _bus.publish(sessionId, event, payload, seq)

HEARTBEAT_SECONDS = 15.0
"""SSE 心跳间隔（秒）：空闲超过即发 `: ping` 注释行保活 / 探测断连。"""

HEARTBEAT_LINE = ": ping\n\n"
"""SSE 注释行（心跳帧；客户端按 SSE 规范忽略）。"""

_REPLAY_MAX_ROWS = 10 * _REPLAY_LIMIT
"""一次回放的**总量**上限（翻页累加），防御无界物化。

单页上限 `_REPLAY_LIMIT` 防不住「错过多页」：`_fetchMissed` 翻页直到取不满一页，
故另设总闸。达到上限即停止翻页并记 WARNING（含 lastSeq），**不**静默截断。
"""


# ---------------------------------------------------------------------------
# 鉴权：可归属身份
# ---------------------------------------------------------------------------


async def _requireResearchUser(
    user: CurrentUser = Depends(getCurrentUser),
) -> CurrentUser:
    """研究域要求可归属身份（dbUserId 非空），否则 401。

    匿名 stub（无头 / 头指向不存在的用户）的 dbUserId 为 None；若放行，
    `created_by=NULL` 的会话会被**任何**另一个匿名调用方认领（NULL == NULL）。
    故此处 fail-closed，与 owner-based ACL 的 `dbUserId` 口径一致。
    """
    if user.dbUserId is None:
        raise AuthFailedError("研究功能需要登录用户：缺少可归属的用户身份")
    return user


# ---------------------------------------------------------------------------
# 生产构造点（Task 6 F1 / Task 6.5 M3）
# ---------------------------------------------------------------------------


def buildResearchAgentService() -> ResearchAgentService:
    """默认构造：真实 ESL / planner / runner / chart + 真 `createClient` + 真 `tokenUsage`
    + 真 `Nl2SqlService`。

    **不注入 `reporter`**：让默认分支产出真实 `ReportPlanner`（Task 6 F1 要的正是
    这条生产路径被执行）。`llmFactory=createClient` 是 key 解析 SSOT，keyless 环境
    由状态机的 `research.error` 显式降级兜底（测试无需 LLM key，见 Task 6.5 M3）。

    `nl2sql=Nl2SqlService()` 是**步 SQL 生成能力的唯一接线点**（Task 6.5-1）：漏注
    会让 `generateStepSql` 直接返回 None，所有计划步落 STEP_MISSING_SQL，`research.step.sql`
    / `.data` / `.chart` / `.done` 事件从不触发（13b 真机铁证 `执行步缺 SQL，按无数据跳过`）。
    守卫见 `test_research_api.py::test_default_construction_uses_real_reporter_and_llm_factory`。
    """
    return ResearchAgentService(
        esl=buildEnterpriseSemanticLayer(),
        sessionService=_sessionService,
        planner=StepQueryPlanner(),
        runner=ResearchSqlRunner(),
        chartService=ChartService(),
        llmFactory=createClient,
        tokenUsage=TokenUsageService(),
        nl2sql=Nl2SqlService(),
    )


def buildEnterpriseSemanticLayer() -> EnterpriseSemanticLayer:
    """三臂真实检索器（设计 §4.6：不调 LLM、不写库、无状态）。

    每臂都是「既有公开服务的薄适配器」——ESL 只认 `list[dict]` 契约，适配层把
    本体 / KPI / wiki 的既有检索产物映射成 ESL 的键名，不改动既有服务。
    """
    return EnterpriseSemanticLayer(
        boSearcher=_searchBusinessObjects,
        kpiMatcher=_matchMetrics,
        wikiSearcher=_searchKnowledge,
    )


async def _searchBusinessObjects(question: str, *, topK: int = _ESL_TOP_K) -> list[dict[str, Any]]:
    """BO 臂：本体类语义检索 + `source_table` 回填。

    回填是必需的：ESL 下游 `eslClasses` 用物理表名给 planner / NL2SQL 做本体提示，
    只给 classId 等于没给上下文。回填用独立短会话（检索器无 session 入参）。
    """
    hits = await OntologyService().searchByKeyword(question, topK=topK, typeFilter="class")
    if not hits:
        return []
    ids = [hit.id for hit in hits]
    async with getSessionFactory()() as session:
        rows = await session.scalars(select(OntologyClass).where(OntologyClass.id.in_(ids)))
        tableById = {row.id: row.source_table for row in rows}
    return [
        {
            "classId": hit.id,
            "className": hit.name,
            "sourceTable": tableById.get(hit.id) or "",
            "matchedAlias": hit.alias or "",
            "confidence": float(hit.score or 0.0),
        }
        for hit in hits
    ]


async def _matchMetrics(question: str, *, topK: int = _ESL_TOP_K) -> list[dict[str, Any]]:
    """Metric 臂：KPI 语义匹配（缓存驱动，无需 session）。"""
    matches = await KpiSemanticMatchService(get_kpi_match_cache()).matchAll(question, limit=topK)
    return [
        {
            "metricId": None,
            "kpiCode": match.code,
            "displayName": match.kpi_name or match.code,
            "formula": None,
            "confidence": float(match.confidence or 0.0),
        }
        for match in matches
    ]


async def _searchKnowledge(question: str, *, topK: int = _ESL_TOP_K) -> list[dict[str, Any]]:
    """Knowledge 臂：wiki 语义检索（Milvus 近邻 + PG 回填标题/正文）。"""
    async with getSessionFactory()() as session:
        hits = await WikiVectorService().searchSemantic(session, question, topK=topK)
    return [
        {
            "pageId": hit.get("pageId"),
            "title": hit.get("title") or "",
            "snippet": hit.get("chunkText") or "",
            "score": float(hit.get("score") or 0.0),
        }
        for hit in hits
    ]


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


@router.post(
    "/sessions",
    response_model=ResearchSessionRead,
    status_code=status.HTTP_201_CREATED,
    summary="新建研究会话",
)
async def createSession(
    payload: ResearchSessionCreate,
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> ResearchSessionRead:
    # 先解析数据源与模型再建行：解析失败（无可用源 / id 不存在 / 模型已停用）不留半成品会话
    ds = await _resolveDatasource(db, payload.datasourceId)
    modelId = await _resolveModelId(db, payload.modelId)
    row = await _sessionService.createSession(
        db,
        userId=user.dbUserId,
        question=payload.question,
        mode=payload.mode,
        datasourceId=ds.id,
        modelId=modelId,
    )
    await db.commit()
    return _sessionRead(row)


async def _resolveDatasource(db: AsyncSession, datasourceId: int | None) -> DataSource:
    """解析研究会话的业务数据源（Task 13e）。

    - 显式 `datasourceId` → `DataSourceService.get`（不存在 → 404 NotFoundError）；
    - 缺省 → **默认数据源**：`list(activeOnly=True)` 首元素（排序为 `is_default.desc(), id`）；
    - 空列表 → `ValidationError`（显式报错，**绝不**静默回落到应用元数据库会话 ——
      研究侧的执行业务 SQL 必须有业务库连接，这正是本特性此前的根因）。
    """
    service = DataSourceService()
    if datasourceId is not None:
        return await service.get(db, datasourceId)
    sources = await service.list(db, activeOnly=True)
    if not sources:
        raise ValidationError(MSG_DATASOURCE_NONE_AVAILABLE)
    return sources[0]


async def _resolveModelId(db: AsyncSession, modelId: int | None) -> int | None:
    """解析研究会话的 LLM 模型（W5）：显式指定 → 校验存在且启用；缺省 → None（自动路由）。

    查**全量**清单（`activeOnly=False`）而非「可用池」：这样「配置存在但已停用」会报
    404，而不是被池过滤掉后误判为「不存在」—— 与 chat 的消费模式同口径
    （`services/chat_service.py:831-845`）。显式报错、**不静默回落自动路由**。
    """
    if modelId is None:
        return None
    configs = await ModelConfigService().list(db, activeOnly=False)
    target = next((config for config in configs if config.id == modelId), None)
    if target is None or not target.is_active:
        raise NotFoundError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=modelId))
    return target.id


@router.get("/sessions", response_model=list[ResearchSessionRead], summary="我的研究会话")
async def listSessions(
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> list[ResearchSessionRead]:
    rows = await db.scalars(
        select(ResearchSession)
        .where(ResearchSession.created_by == user.dbUserId)
        .order_by(ResearchSession.created_at.desc(), ResearchSession.id.desc())
    )
    return [_sessionRead(row) for row in rows]


@router.delete(
    "/sessions/{sessionId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除研究会话",
)
async def deleteSession(
    sessionId: uuid.UUID,
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """硬删会话，级联清 turn / checkpoint / finding / report（复用 DB CASCADE）。

    归属不符 → 404（而非 403）：与详情 / 列表同一口径，不泄露会话存在性。
    """
    await assert_research_session_ownership(db, sessionId, user)
    removed = await _sessionService.deleteSession(db, sessionId)
    if removed == 0:
        # 归属已过仍删 0 行 = 并发下已被另一请求删掉；语义上仍是「不存在」。
        # review M2：复用 session_claim 守卫的常量，避免文案漂移。
        raise NotFoundError(_MSG_RESEARCH_SESSION_NOT_FOUND)
    await db.commit()


@router.get(
    "/sessions/{sessionId}",
    response_model=ResearchSessionDetail,
    summary="会话详情（含轮次与待决策点）",
)
async def getSessionDetail(
    sessionId: uuid.UUID,
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> ResearchSessionDetail:
    row = await assert_research_session_ownership(db, sessionId, user)
    turns = await db.scalars(
        select(ResearchTurn)
        .where(ResearchTurn.session_id == sessionId)
        .order_by(ResearchTurn.turn_index.asc())
    )
    pending = await _sessionService.getPendingCheckpoint(db, sessionId)
    return ResearchSessionDetail(
        session=_sessionRead(row),
        turns=[_turnRead(turn) for turn in turns],
        pendingCheckpoint=_checkpointRead(pending) if pending is not None else None,
    )


@router.post(
    "/sessions/{sessionId}/turns",
    response_model=ResearchTurnAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="追加一轮研究（状态机后台跑，进度走 SSE）",
)
async def createTurn(
    sessionId: uuid.UUID,
    payload: ResearchTurnCreate,
    background: BackgroundTasks,
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> ResearchTurnAccepted:
    """追加一轮研究（202）；**会话级互斥**：已有进行中的轮次 ⇒ 409。

    Task 2 两句判定按**这个顺序**（不可互换）：

    1. **幂等键命中**优先 —— 重试（同 `idempotencyKey`）必须回放**首次**的 turnId，
       而不是被自己首次提交占住的会话弹 409；命中即返回，不再写新轮次；
    2. 否则 `active_run_id` 非空 ⇒ 409。状态机在后台跑、且**只有终态**才释放执行权
       （暂停等用户决策时仍占用），故这里读到非空就说明上一轮还没结束。

    P0-X5（1012 cross-refs spec §P0-X5）：归属 + claim invariant 双断言下沉
    到 ``app.services.session_claim`` —— ``assert_research_claim_invariant(
    expected="free")`` 替换原本内联的 `row.active_run_id is not None` 判定。
    """
    row = await assert_research_session_ownership(db, sessionId, user)
    if payload.idempotencyKey:
        replay = await _sessionService.getRunStateByIdempotencyKey(
            db, sessionId, payload.idempotencyKey
        )
        if replay is not None:
            logger.info(
                "幂等键命中，回放首次轮次: session=%s key=%s turn=%s",
                sessionId,
                payload.idempotencyKey,
                replay.turn_id,
            )
            return ResearchTurnAccepted(
                sessionId=sessionId, turnId=replay.turn_id, status=row.status
            )
    # P0-X5：claim invariant ``free`` 断言（无活跃轮次才放行；不达 ⇒ 409）。
    # 408/409 文案由 ``assert_research_claim_invariant`` 单点给出，本端点
    # 不再维护 ``_busyMessage`` 之类的本地分支文案（避免与守卫文案漂移）。
    await assert_research_claim_invariant(db, sessionId, expected="free")
    turn = await _sessionService.appendTurn(
        db, sessionId=sessionId, role="user", content={"question": payload.question}
    )
    # 先取原始值再 commit（不依赖 expire_on_commit 语义，避免提交后 lazy-load）
    turnId, mode, userId = turn.id, row.mode, user.dbUserId
    # 台账入队必须在 commit **之前**：入队与 turn 同事务落地，后台任务才 claim 得到。
    task = await enqueueTask(db, sessionId=sessionId, turnId=turnId, kind=TASK_KIND_TURN)
    taskId = str(task.id)
    await db.commit()
    background.add_task(
        _runTurnInBackground,
        taskId,
        str(sessionId),
        str(turnId),
        payload.question,
        userId,
        mode,
        payload.idempotencyKey,
    )
    return ResearchTurnAccepted(sessionId=sessionId, turnId=turnId, status="running")


@router.post(
    "/sessions/{sessionId}/abort",
    response_model=ResearchSessionAbortRead,
    summary="取消会话（释放执行权并关流）",
)
async def abortSession(
    sessionId: uuid.UUID,
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> ResearchSessionAbortRead:
    """取消研究会话：落 `aborted` 终态、释放执行权，并广播**终态**事件关掉 SSE 流。

    Task 2（review Fix 1b）：这是「轮次停在检查点等用户决策、用户却不想再等」的唯一出路。
    在此之前该状态会**永久**占用 `active_run_id` —— 用户此后每次提交都 409，且没有任何
    端点能摘掉这个占用（唯一出路是删掉整个会话）。

    三种结局（状态谓词在**服务层 SQL 里**，不只在这里 `if` —— 双保险）：

    1. **`awaiting_user` ⇒ 200 `cancelled=true`**，落 `aborted` + 释放执行权 + 发一帧终态。
       这是唯一「占用在手但没有任务在跑」的状态（review Fix A 的白名单）。
    2. **`running` ⇒ 409**，状态与执行权**都不动**、**不发帧**。真在跑的会话被 abort 会
       （a）被状态机的无条件终态写入复活、（b）清空占用后放进第二条状态机（见
       `abortSessionIfActive` docstring）。**不发帧**很关键：终态帧会关掉一条**活着的**流。
    3. **已终态 ⇒ 200 `cancelled=false`**，状态原样（`done` 不得被翻成 `aborted` 丢结果）。

    响应带**服务层读回的库中真值**（列查询绕过 identity map），端点**不再**回读、故也
    不存在「先改后 404」的路径；`None`（行已消失）只会出现在「什么都没改」时。
    """
    await assert_research_session_ownership(db, sessionId, user)
    status = await _sessionService.abortSessionIfActive(db, sessionId)
    if status is None:
        # UPDATE 未命中且行已不在（与 delete 竞态）：本请求**无副作用**，故与其余端点
        # 同一口径报「不存在」—— 不是「先改后 404」。
        # review M2：复用 session_claim 守卫的常量。
        raise NotFoundError(_MSG_RESEARCH_SESSION_NOT_FOUND)
    if status == STATUS_RUNNING:
        logger.info("拒绝取消（会话正在执行）: session=%s", sessionId)
        raise ConflictError(_ABORT_RUNNING_MESSAGE)
    await db.commit()
    if status == "aborted":
        # 复用既有终态码 turn_failed（`TERMINAL_ERROR_CODES` 成员 ⇒ `_isTerminal` 认它关流）。
        # **已知错配**（review Fix C）：`ERROR_SPECS[ERROR_TURN_FAILED].sessionStatus` 是
        # `failed`，而这里会话落的是 `aborted` —— 该表对本码的描述**低于**实际情况。
        # 之所以现在功能正确：运行时**无人**消费 ErrorSpec.sessionStatus（`_isTerminal` 只问
        # 「是不是终态码」）；它是给前端/文档看的声明。不新增错误码（新码必须同批扩前端
        # union/i18n/store，而本端点尚无前端调用方）—— 等接线时一并校正该表。
        # 终态 error 也必须可回放（Task 12）：走 `_persistAndPublish`（先落 outbox 再广播）。
        await _persistAndPublish(
            str(sessionId),
            EVENT_ERROR,
            errorPayload(ERROR_TURN_FAILED, "本轮研究已取消", phase=None),
        )
    logger.info("取消研究会话: session=%s status=%s cancelled=%s", sessionId, status, status == "aborted")
    return ResearchSessionAbortRead(sessionStatus=status, cancelled=status == "aborted")


@router.post(
    "/checkpoints/{checkpointId}/answer",
    response_model=CheckpointAnswerRead,
    summary="提交检查点决策（状态机后台续跑）",
)
async def answerCheckpoint(
    checkpointId: uuid.UUID,
    payload: CheckpointAnswerRequest,
    background: BackgroundTasks,
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> CheckpointAnswerRead:
    checkpoint = await db.get(ResearchCheckpoint, checkpointId)
    if checkpoint is None:
        raise NotFoundError("研究检查点不存在")
    sessionRow = await assert_research_session_ownership(db, checkpoint.session_id, user)  # 越权同样 404，不泄露存在性
    # P0-X5：claim invariant ``held`` 断言 —— 续跑（resume）只对仍有活跃轮次的
    # 会话开放。abort 之后会话落 ``aborted`` + ``active_run_id`` 清空，但
    # pending checkpoint 仍 ``status="pending"``；不挡则 resumeTurn 把会话拉
    # 回 running 而不 claim ⇒ 出现 ``running + active_run_id=NULL`` 的会话，
    # 下次 POST /turns 直接放行、起第二条状态机。详见模块 docstring。
    await assert_research_claim_invariant(db, checkpoint.session_id, expected="held")
    if checkpoint.status != "pending":
        if not _isIdempotentReplay(checkpoint, payload):
            raise ConflictError(f"检查点已决策（{checkpoint.status}），不可重复提交")
        # 幂等回放：同一 (action, choice) 的重复提交（双击 / 超时重试）视为已被接受，
        # 返回与原提交**相同**的 nextPhase，而不是 409 把用户当成冲突。
        # 会话状态取**库中当前值**：首次决策后状态机可能已跑到终态（回放报 running 会说谎）。
        replayPhase = decideResume(checkpoint, payload.action, dict(payload.choice or {})).phase
        logger.info("检查点决策幂等回放: checkpoint=%s action=%s", checkpointId, payload.action)
        return CheckpointAnswerRead(sessionStatus=sessionRow.status, nextPhase=replayPhase)
    nextPhaseValue = decideResume(checkpoint, payload.action, dict(payload.choice or {})).phase
    choice = dict(payload.choice or {})
    # 台账入队：resume 由 checkpoint 驱动（turn_id 取 checkpoint.turn_id）。**注意**：
    # `decideResume` 是纯函数，入队是本 handler 的**第一处写** —— getDb 只在异常时
    # rollback、**从不 commit**（infrastructure/database.py），故这里必须**自己 commit**，
    # 否则后台 `_resumeTurnInBackground` 自开的会话看不到 task 行、`claimTask` 恒 False。
    task = await enqueueTask(
        db,
        sessionId=checkpoint.session_id,
        turnId=checkpoint.turn_id,
        kind=TASK_KIND_RESUME,
        payload={
            "checkpointId": str(checkpoint.id),
            "action": payload.action,
            "choice": choice,
            "modelId": payload.model_id,
        },
    )
    # commit 会 expire ORM 对象 ⇒ **先快照**返回值要用的 sessionStatus，避免提交后
    # 异步懒加载（与 createTurn 的 turnId/mode/userId 快照同款）。
    statusSnapshot = sessionRow.status
    taskId, checkpointId, checkpointSessionId = str(task.id), str(checkpoint.id), str(checkpoint.session_id)
    await db.commit()
    background.add_task(
        _resumeTurnInBackground,
        taskId,
        checkpointId,
        checkpointSessionId,
        payload.action,
        choice,
        payload.model_id,
    )
    # 报**库中真实**状态（与上面的幂等回放分支同口径，review Fix E）：硬编码 "running" 报的
    # 是「状态机接下来要去的状态」，不是此刻的事实 —— 此刻会话正 `awaiting_user`（正因为
    # 它停在检查点，才存在这个 pending checkpoint 可答）。
    return CheckpointAnswerRead(sessionStatus=statusSnapshot, nextPhase=nextPhaseValue)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/{sessionId}/report",
    response_model=ResearchReportRead,
    summary="当前（或指定版本）研究报告",
)
async def getReport(
    sessionId: uuid.UUID,
    version: int | None = Query(default=None, ge=1),
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> ResearchReportRead:
    await assert_research_session_ownership(db, sessionId, user)
    reports = await _sessionService.listReports(db, sessionId)
    target = _pickReport(reports, version)
    if target is None:
        raise NotFoundError("研究报告不存在")
    return _reportRead(target)


@router.get(
    "/sessions/{sessionId}/reports",
    response_model=list[ResearchReportSummaryRead],
    summary="报告版本列表",
)
async def listReports(
    sessionId: uuid.UUID,
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> list[ResearchReportSummaryRead]:
    await assert_research_session_ownership(db, sessionId, user)
    reports = await _sessionService.listReports(db, sessionId)
    return [_reportSummary(report) for report in reports]


# ---------------------------------------------------------------------------
# SSE：research.* 事件流（Task 8，独立端点）
# ---------------------------------------------------------------------------


@router.get("/stream", summary="研究进度 SSE（research.* 事件族）")
async def streamSessionEvents(
    sessionId: uuid.UUID,
    lastEventId: int | None = Query(
        default=None, description="上次收到的事件 seq；只回放其后的"
    ),
    user: CurrentUser = Depends(_requireResearchUser),
) -> StreamingResponse:
    """订阅本会话的进度事件（设计 §4.5）。

    **支持历史回放**（Task 12）：带 `?lastEventId=<seq>` 时先回放该 `seq` **之后**的
    已落库事件（每帧带 `id: <seq>`），再转入实时推送 —— 页面刷新 / SSE 断线后不再永久
    丢失错过的事件。不带游标则**不**回放（无「错过」的概念，避免把陈年事件当新事件重放）。

    时序建议：首次连接先订阅、再触发 turn；重连时带上客户端记住的最后一个 `id`。
    鉴权与越权同 REST 口径：他人会话 404（不泄露存在性）。归属校验用**短会话**（流是
    长连接，不能把请求级 session 拖到流结束）。
    """
    async with getSessionFactory()() as db:
        await assert_research_session_ownership(db, sessionId, user)
    return StreamingResponse(
        _eventFrames(str(sessionId), lastEventId),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _eventFrames(sessionId: str, lastEventId: int | None = None) -> AsyncIterator[str]:
    """队列 → SSE 帧：首帧 connected；可选回放错失事件；空闲发心跳；终态或断连后收尾退订。

    **先订阅再回放**（Task 12）：先 `subscribe` 再查 outbox，否则「查完到订阅」之间新发的
    事件会漏。回放帧带 `id:`，实时帧若已落库也带 ⇒ 客户端按 `seq` 去重。

    **回放收尾的两个条件**（Task 12 fix1，缺一不可）：回放的**最后一帧**是终态 **且**
    会话**当前状态**是终态。outbox 按**会话**累计、无 run 作用域，而 `_isTerminal` 认
    **任意** `research.done` —— 多轮会话里上一轮的 `done` 就在回放中间，若循环里撞到终态
    就 `return`，**当前轮**的事件将一条都收不到（UI 永远转圈）。故循环里**不** return，
    回放完统一判一次。

    `frames` 为空的判据不能省：游标已最新（回放为空）是「对已结束的会话先连接、再 POST
    新 turn」的正常前奏 —— 此时若只看会话状态就会把连接误关。
    """
    queue = _bus.subscribe(sessionId)
    try:
        yield _sseFrame(EVENT_CONNECTED, {"sessionId": sessionId})
        if lastEventId is not None:
            frames = await _fetchMissed(sessionId, lastEventId)
            lastTerminal = False
            for event, payload, seq in frames:
                yield _sseFrame(event, payload, seq)
                lastTerminal = _isTerminal(event, payload)  # 只保留**最后一帧**的值
            if frames and lastTerminal and await _isSessionTerminal(sessionId):
                return
        while True:
            try:
                event, payload, seq = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                yield HEARTBEAT_LINE
                continue
            yield _sseFrame(event, payload, seq)
            if _isTerminal(event, payload):
                return
    finally:
        # 客户端断连（生成器被取消）与正常收尾都走这里：不留下悬空订阅
        _bus.unsubscribe(sessionId, queue)


async def _fetchMissed(
    sessionId: str, lastEventId: int, limit: int | None = None
) -> list[tuple[str, dict[str, Any], int]]:
    """独立短会话取 `seq > lastEventId` 的**全部**事件，物化为纯元组（脱离 ORM 会话）。

    用**独立短会话**：流是长连接，不能把请求级 session 拖到流结束；也避免会话关闭后访问
    已 detach 行属性。取数失败只留痕、返回空 —— 回放失败不该炸掉实时流。

    **翻页直到取不满一页**（Task 12 fix1）：单页查询在错过的事件 > 单页上限时，尾部既不在
    回放页里、也不在订阅队列里（它们在订阅之前就已发布）⇒ **静默丢失**。`limit`（单页大小）
    可注入以便用小页覆盖翻页；`None` ⇒ 用 `_REPLAY_LIMIT`（取模块全局，便于测试改写）。
    """
    pageSize = _REPLAY_LIMIT if limit is None else limit
    try:
        async with getSessionFactory()() as session:
            return await _collectReplayRows(
                session, uuid.UUID(sessionId), lastEventId, pageSize
            )
    except Exception:  # noqa: BLE001 —— 回放失败只留痕，不炸流（实时事件仍照常投递）
        logger.exception("SSE 历史回放失败: session=%s afterSeq=%s", sessionId, lastEventId)
        return []


async def _collectReplayRows(
    session: AsyncSession, sessionId: uuid.UUID, lastEventId: int, pageSize: int
) -> list[tuple[str, dict[str, Any], int]]:
    """按 `seq` 游标翻页收集，直到取不满一页或达到 `_REPLAY_MAX_ROWS` 总量闸。"""
    collected: list[tuple[str, dict[str, Any], int]] = []
    cursor = lastEventId
    while True:
        rows = await fetchEventsAfter(session, sessionId, cursor, pageSize)
        if not rows:
            break
        collected.extend((row.event, row.payload, row.seq) for row in rows)
        cursor = rows[-1].seq
        if len(rows) < pageSize or len(collected) >= _REPLAY_MAX_ROWS:
            break
    if len(collected) >= _REPLAY_MAX_ROWS:
        logger.warning(
            "SSE 回放达到总量上限 %s，尾部可能被截断: session=%s lastSeq=%s",
            _REPLAY_MAX_ROWS,
            sessionId,
            cursor,
        )
    return collected


async def _isSessionTerminal(sessionId: str) -> bool:
    """会话**当前**是否终态（`done`/`failed`/`aborted`）—— 回放收尾的第二个条件。

    必须与「回放最后一帧是终态」同时成立才收尾（见 `_eventFrames`）：outbox 按会话累计、
    `_isTerminal` 认任意终态帧，只看回放会把上一轮的 `done` 当成收尾信号。

    **`awaiting_user` 不是终态**（复用 `TERMINAL_SESSION_STATUSES`）：会话在等用户答
    checkpoint，状态机稍后会续跑 —— 当成终态会让「等待用户输入」的流瞬间被关。

    读失败（异常 / 行不存在）⇒ 记 WARNING 并返回 `False`：**宁可多连一会儿，不可误关流**。
    用独立短会话只读、不 commit（流是长连接，不能拖住请求级 session）。
    """
    try:
        async with getSessionFactory()() as session:
            status = await session.scalar(
                select(ResearchSession.status).where(
                    ResearchSession.id == uuid.UUID(sessionId)
                )
            )
    except Exception:  # noqa: BLE001 —— 状态读失败不得误关流
        logger.warning("会话终态判定失败，按非终态处理: session=%s", sessionId, exc_info=True)
        return False
    if status is None:
        logger.warning("会话终态判定：行不存在，按非终态处理: session=%s", sessionId)
        return False
    return status in TERMINAL_SESSION_STATUSES


def _sseFrame(event: str, payload: dict[str, Any], seq: int | None = None) -> str:
    """SSE 帧（`research_json` 转义换行，payload 不会破坏帧边界）。

    `seq is not None` 时在 `event:` 行后加 `id: {seq}` 行（SSE 规范：`event:` / `id:` /
    `data:` 任意顺序，`data:` 收尾）—— 客户端据此记住游标、重连带 `?lastEventId=`。

    payload 里有业务库驱动标量（Decimal / date / datetime / UUID）时走 `research_json`
    的 JSON 安全映射（Task 3）。**序列化有守卫**（Task 12）：`research_json` 对白名单外
    类型（`bytes`/LOB…）抛错，若任由异常冒泡，一帧坏值会炸掉**整条流**。故先试
    `research_json`，失败降级 `_coerceJsonSafe`（未知类型 → str），再失败丢该帧并记 ERROR。
    """
    data = _encodeFrameData(event, payload)
    if data is None:
        return ""
    lines = [f"event: {event}"]
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


def _encodeFrameData(event: str, payload: dict[str, Any]) -> str | None:
    """帧 `data:` 段编码：`research_json` 优先；失败降级 `_coerceJsonSafe`；再失败丢帧。

    两段守卫都**记 ERROR 且不外抛**：一帧坏值不得截断整条流（降级帧仍用原事件名，
    **不**新造 error code —— 新 code 会强制前端 union/i18n/store 同批改动）。
    """
    try:
        return research_json(payload)
    except Exception:  # noqa: BLE001 —— 白名单外类型（bytes/LOB…）：降级，不炸流
        logger.exception("SSE 帧序列化失败，降级 _coerceJsonSafe: event=%s", event)
    try:
        return json.dumps(_coerceJsonSafe(payload), ensure_ascii=False, allow_nan=False)
    except Exception:  # noqa: BLE001 —— 降级仍失败：丢该帧，不炸流
        logger.exception("SSE 帧降级序列化仍失败，丢弃该帧: event=%s", event)
        return None


def _isTerminal(event: str, payload: dict[str, Any]) -> bool:
    """终态判定：`research.done` 或**终态**错误码（降级类 error 不关流）。

    终态集合由 `research_agent_ports.ERROR_SPECS` 派生（`TERMINAL_ERROR_CODES`），
    只有 `turn_failed` 关流；降级类（llm_unavailable / 步失败等）不关流，否则客户端
    会丢掉后续事件（含 `research.done`）。
    """
    if event == EVENT_DONE:
        return True
    return event == EVENT_ERROR and payload.get("code") in TERMINAL_ERROR_CODES


# ---------------------------------------------------------------------------
# 后台任务（自开会话；状态机自带 commit 是持久化机制）
# ---------------------------------------------------------------------------


async def _runTurnInBackground(
    taskId: str,
    sessionId: str,
    turnId: str,
    question: str,
    userId: int,
    mode: str,
    idempotencyKey: str | None = None,
) -> None:
    """后台跑状态机：自开会话 → 认领台账 → `runTurn` → 收尾 commit。

    异常已在状态机内 `research.error` + 会话置 failed；此处只兜底 rollback 与日志
    （后台任务不得把异常抛回响应 —— 那会让 202 变成 500）。

    `emit` 接线（Task 8 / 12）：`partial(_persistAndPublish, sessionId)` —— 状态机发的事件
    **先落** `research_event_outbox`（带 `seq`）**再**广播给已订阅的流端；错过的流端可用
    `?lastEventId=` 回放（订阅仍建议先于本任务，以最小化回放窗口）。

    `idempotencyKey`（Task 2）：透传进 `runTurn` 落 run state，供重试回放同一 turnId。

    台账（Task 10+11）：认领走原子条件 UPDATE，**认领后立即 commit** —— 否则 run 抛异常
    时 rollback 会把租赁一并撤掉，reaper 就看不到「有任务在跑」。收尾：`done`（含
    `awaiting_user` 暂停，本单元工作已完成）与 `failed`。

    执行权冲突（`RunClaimConflictError`）：**不得**走 `_markTerminalFailure` ——
    另一条运行正在正常推进，标 failed 会毁掉它的 UI。与 checkpoint 冲突同款：
    发 `research.error(checkpoint_conflict)`（uiHint=conflict）告知用户「另一轮在跑」。
    台账本条落 `failed` 是对的（本次没干活），但**不动**会话状态。
    """
    service = buildResearchAgentService()
    async with getSessionFactory()() as session:
        if not await claimTask(session, taskId=uuid.UUID(taskId), workerId=_WORKER_ID):
            logger.warning("研究任务认领未命中，跳过本轮: task=%s session=%s", taskId, sessionId)
            return
        await session.commit()  # 租赁先落地：后续 rollback 不得把它撤掉
        try:
            await service.runTurn(
                session,
                sessionId=uuid.UUID(sessionId),
                turnId=uuid.UUID(turnId),
                question=question,
                userId=userId,
                mode=mode,
                emit=partial(_persistAndPublish, sessionId),
                idempotencyKey=idempotencyKey,
            )
            await finishTask(session, taskId=uuid.UUID(taskId), status=TASK_STATUS_DONE)
            await session.commit()
        except RunClaimConflictError as exc:
            logger.warning("会话已有进行中的轮次，本轮放弃: session=%s turn=%s", sessionId, turnId)
            await session.rollback()
            await _finishTaskQuietly(session, taskId, str(exc))
            await _persistAndPublish(
                sessionId, EVENT_ERROR, errorPayload(ERROR_CHECKPOINT_CONFLICT, str(exc))
            )
        except Exception as exc:  # noqa: BLE001 —— 后台任务兜底：留痕 + 回滚，不上抛
            logger.exception("研究 turn 后台执行失败: session=%s turn=%s", sessionId, turnId)
            await session.rollback()
            await _markTerminalFailure(
                service, session, sessionId, str(exc), taskId=uuid.UUID(taskId)
            )


async def _resumeTurnInBackground(
    taskId: str,
    checkpointId: str,
    sessionId: str,
    action: str,
    choice: dict[str, Any],
    modelId: int | None = None,
) -> None:
    """后台按用户决策续跑状态机（resolveCheckpoint 在 `resumeTurn` 内完成）。

    `sessionId` 由调用方显式传入（Task 8）：`emit` 必须绑定会话键才能投到正确的流。

    台账（Task 10+11）：认领 + 立即 commit（同 `_runTurnInBackground`）；三种异常分支
    都把本条落 `failed`（本次单元没干成活）——`CheckpointConflictError` 用新事务
    （该分支**不** rollback，见下）。

    并发冲突（CheckpointConflictError）：抛出 409 事件，不落 failed 状态——第二次
    提交的用户看到的是 SSE 流里的 error.event（前端据此弹 409 提示），而不是静默
    500 或会话被标记为 failed。

    **过期**（CheckpointStaleError）另当别论：该检查点再也无法 resolve、会话也永远跑不到
    终态 ⇒ 必须显式落终态并释放执行权（`_abortExpiredSession`），否则 `active_run_id`
    永久占用、用户此后每次提交都 409（且没有 abort 端点，唯一出路是删掉整个会话）。
    """
    service = buildResearchAgentService()
    async with getSessionFactory()() as session:
        if not await claimTask(session, taskId=uuid.UUID(taskId), workerId=_WORKER_ID):
            logger.warning("研究任务认领未命中，跳过续跑: task=%s checkpoint=%s", taskId, checkpointId)
            return
        await session.commit()  # 租赁先落地
        try:
            await service.resumeTurn(
                session,
                checkpointId=uuid.UUID(checkpointId),
                action=action,
                choice=choice,
                model_id=modelId,
                emit=partial(_persistAndPublish, sessionId),
            )
            await finishTask(session, taskId=uuid.UUID(taskId), status=TASK_STATUS_DONE)
            await session.commit()
        except CheckpointStaleError as exc:
            logger.warning("checkpoint 已过期，会话落终态: checkpoint=%s", checkpointId)
            await _persistAndPublish(
                sessionId,
                EVENT_ERROR,
                errorPayload(ERROR_CHECKPOINT_CONFLICT, str(exc)),
            )
            await session.rollback()
            await _abortExpiredSession(session, sessionId)
            await _finishTaskQuietly(session, taskId, str(exc))
        except CheckpointConflictError as exc:
            # 并发提交：另一请求已抢先决策 ⇒ 那条 resume 正在正常推进，会在终态自行释放，
            # 故这里**不动**执行权（释放了反而放并发进来）。该分支不 rollback ⇒ 事务可能
            # 仍开着：先 rollback 关掉它，再在新事务里给台账收尾。
            logger.warning("checkpoint 决策冲突，转 409: checkpoint=%s action=%s", checkpointId, action)
            await _persistAndPublish(
                sessionId,
                EVENT_ERROR,
                errorPayload(ERROR_CHECKPOINT_CONFLICT, str(exc)),
            )
            await session.rollback()
            await _finishTaskQuietly(session, taskId, str(exc))
        except Exception as exc:  # noqa: BLE001 —— 未预期异常：留痕 + 回滚
            logger.exception("研究 turn 恢复失败: checkpoint=%s action=%s", checkpointId, action)
            await session.rollback()
            await _markTerminalFailure(
                service, session, sessionId, str(exc), taskId=uuid.UUID(taskId)
            )


async def _abortExpiredSession(session: AsyncSession, sessionId: str) -> None:
    """检查点过期 ⇒ 会话落 `aborted` 终态**并**释放执行权（否则永久 409，见调用点）。

    调用方已 `rollback`，故本函数在**新事务**里落库并自行 commit；失败只留痕 —— 该路径
    是善后，不该反过来掩盖已经通过 SSE 发给用户的冲突原因。

    **善后未生效必须留信号**（review Fix G）：`abortSessionIfActive` 的白名单只有
    `awaiting_user`，若这条路径上会话竟是别的状态，取消会变成一个**静默 no-op**
    （只留一句 info 日志）而执行权继续占着 —— 正是本任务要修的死路，却没有告警。
    故非 `aborted` 一律 WARNING。**只告警不上抛**：本函数在善后路径上，抛异常会掩盖
    已通过 SSE 发给用户的原始冲突原因。
    """
    try:
        status = await _sessionService.abortSessionIfActive(session, uuid.UUID(sessionId))
        if status != "aborted":
            logger.warning(
                "过期检查点的善后未生效（期望 awaiting_user ⇒ aborted）: session=%s status=%s",
                sessionId,
                status,
            )
        await session.commit()
    except Exception:  # noqa: BLE001 —— 善后失败只留痕，不掩盖原始冲突
        logger.exception("过期检查点落 aborted 失败: session=%s", sessionId)


async def _markTerminalFailure(
    service: ResearchAgentService,
    session: AsyncSession,
    sessionId: str,
    message: str,
    taskId: uuid.UUID | None = None,
) -> None:
    """后台 wrapper 兜底：补发终态 error（关流）+ 会话落 failed + 台账收尾（同一事务）。

    **单一终态路径**：本函数只服务「后台 wrapper 捕获到未预期异常」，code 恒为终态码
    `ERROR_TURN_FAILED`。Task 14 / N4 移除的旧写法 `if code not in TERMINAL_ERROR_CODES:
    return` 因 code 恰是终态字面量而**恒假**——与 docstring「不硬编码字面量」一起说谎，是死分支；
    集合判定只保留在 `_isTerminal`（事件侧决定是否关流）。rollback 后写库失败只留痕、
    不掩盖原始异常（原始异常已由调用方 `logger.exception` 留痕）。

    台账（Task 10+11）：`taskId` 传入时，`finishTask(failed)` 与 `markFailed` 挂进**同一次**
    提交（`markFailed` 只 UPDATE 会话、不 commit）—— 会话落 failed 与台账落 failed 同生共死，
    不留「会话已终态、台账仍 leased ⇒ 被下轮 reaper 误当孤儿」的窗口。
    """
    await _persistAndPublish(
        sessionId, EVENT_ERROR, errorPayload(ERROR_TURN_FAILED, message, phase=None)
    )
    try:
        await service.markFailed(session, uuid.UUID(sessionId))
        if taskId is not None:
            await finishTask(session, taskId=taskId, status=TASK_STATUS_FAILED, error=message)
        await session.commit()
    except Exception:  # noqa: BLE001 —— rollback 后写库失败只留痕，不掩盖原始异常
        logger.exception("终态错误落 failed 失败: session=%s", sessionId)


async def _finishTaskQuietly(session: AsyncSession, taskId: str, message: str) -> None:
    """在**新事务**里把台账落 `failed`；失败只留痕、不上抛（善后路径）。

    调用方已（或就地）`rollback`，故这里自开新事务并自行 commit —— 与 `_abortExpiredSession`
    同款「善后自开事务」写法。用于 `RunClaimConflictError` / `CheckpointConflictError` /
    `CheckpointStaleError` 三条**不该动会话状态**（或不走 `_markTerminalFailure`）的分支。
    """
    try:
        await finishTask(session, taskId=uuid.UUID(taskId), status=TASK_STATUS_FAILED, error=message)
        await session.commit()
    except Exception:  # noqa: BLE001 —— 善后失败只留痕，不掩盖已通过 SSE 发给用户的原因
        logger.exception("任务台账落 failed 失败: task=%s", taskId)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


_STATUS_TO_ACTION: dict[str, str] = {status: action for action, status in ACTION_STATUS.items()}
"""checkpoint 决策状态 → 动作反查（由 `ACTION_STATUS` 派生，不另持字面量）。"""


_ABORT_RUNNING_MESSAGE = "当前轮次正在执行，无法取消；请等待本轮结束后再试"
"""abort 在 `running` 状态被拒的文案（真在跑：状态与执行权都不动，也不发帧）。"""


# P0-X5：``_busyMessage`` 已废弃 —— claim invariant 的 409 文案由
# ``assert_research_claim_invariant`` 单点给出（``app.services.session_claim``）。
# 不再在本端点维护「在跑 / 等决策」分支文案，避免与守卫文案漂移。


def _isIdempotentReplay(
    checkpoint: ResearchCheckpoint, payload: CheckpointAnswerRequest
) -> bool:
    """已决策的 checkpoint 是否收到**完全相同**决策的重复提交（Task 2 幂等回放）。

    判据是库中真实决策（`status` 反查 action + `user_choice`）与请求体逐字相等。
    决策**不同**（先 confirm 再 reject）不算回放 —— 那仍是并发冲突，必须 409，
    否则幂等通道会变成「改写已落库决策」的后门。
    """
    recordedAction = _STATUS_TO_ACTION.get(checkpoint.status)
    if recordedAction is None or recordedAction != payload.action:
        return False
    return dict(checkpoint.user_choice or {}) == dict(payload.choice or {})


# P0-X5：``_ownedSession`` 已提升到 ``app.services.session_claim``。
# 所有研究域写路径（createTurn / answerCheckpoint / abortSession /
# deleteSession / getSession / listReports / stream）统一走
# ``assert_research_session_ownership``；互斥判定走
# ``assert_research_claim_invariant``。本地不再保留。


def _pickReport(reports: list[ResearchReport], version: int | None) -> ResearchReport | None:
    """指定版本 → 精确匹配；未指定 → 当前 published（唯一）。"""
    if version is not None:
        return next((report for report in reports if report.version == version), None)
    return next((report for report in reports if report.status == "published"), None)


def _sessionRead(row: ResearchSession) -> ResearchSessionRead:
    """ORM → DTO（`question` 取 `input_seed`，会话问题无独立列）。"""
    return ResearchSessionRead(
        id=row.id,
        title=row.title,
        mode=row.mode,
        status=row.status,
        question=row.input_seed or "",
        datasourceId=row.datasource_id,
        modelId=row.model_id,
        createdAt=row.created_at,
        updatedAt=row.updated_at,
    )


def _turnRead(row: ResearchTurn) -> ResearchTurnRead:
    return ResearchTurnRead(
        id=row.id,
        turnIndex=row.turn_index,
        role=row.role,
        content=row.content or {},
        createdAt=row.created_at,
    )


def _checkpointRead(row: ResearchCheckpoint) -> ResearchCheckpointRead:
    """`prompt` 从 `options[PROMPT_KEY]` 派生（checkpoint 无 prompt 列，Task 3 裁定 #1）。

    X3：``originalQuestion`` 直接从 row 读（恢复阶段优先用它，不再 join turn.content）。
    """
    options = row.options or {}
    return ResearchCheckpointRead(
        id=row.id,
        phase=row.phase,
        status=row.status,
        options=options,
        prompt=str(options.get(PROMPT_KEY) or ""),
        userChoice=row.user_choice,
        decidedAt=row.decided_at,
        originalQuestion=row.original_question or "",
    )


def _reportRead(row: ResearchReport) -> ResearchReportRead:
    return ResearchReportRead(
        id=row.id,
        version=row.version,
        status=row.status,
        payload=row.payload or {},
        renderedMd=row.rendered_md or "",
        createdAt=row.created_at,
    )


def _reportSummary(row: ResearchReport) -> ResearchReportSummaryRead:
    return ResearchReportSummaryRead(
        id=row.id, version=row.version, status=row.status, createdAt=row.created_at
    )
