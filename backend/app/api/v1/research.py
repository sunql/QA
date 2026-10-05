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

SSE 通道（Task 8）：状态机的事件经 `emit` 接线投递到进程内 `ResearchEventBus`（见
`_runTurnInBackground` / `_resumeTurnInBackground` 的 `functools.partial(bus.publish, ...)`），
流端先订阅再返回 `StreamingResponse`。与 chat 的 SSE 实现**完全独立**（设计 §4.5）。

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
from app.services.messages_zh import MSG_DATASOURCE_NONE_AVAILABLE
from app.services.nl2sql_service import Nl2SqlService
from app.services.ontology_service import OntologyService
from app.services.research_agent_ports import (
    ERROR_TURN_FAILED,
    EVENT_DONE,
    EVENT_ERROR,
    TERMINAL_ERROR_CODES,
    errorPayload,
    nextPhase,
)
from app.services.research_agent_service import ResearchAgentService
from app.services.research_event_bus import EVENT_CONNECTED, bus
from app.services.research_session_service import PROMPT_KEY, ResearchSessionService
from app.services.research_sql_runner import ResearchSqlRunner
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

HEARTBEAT_SECONDS = 15.0
"""SSE 心跳间隔（秒）：空闲超过即发 `: ping` 注释行保活 / 探测断连。"""

HEARTBEAT_LINE = ": ping\n\n"
"""SSE 注释行（心跳帧；客户端按 SSE 规范忽略）。"""


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
    # 先解析数据源再建行：解析失败（无可用源 / id 不存在）不留无源的半成品会话
    ds = await _resolveDatasource(db, payload.datasourceId)
    row = await _sessionService.createSession(
        db,
        userId=user.dbUserId,
        question=payload.question,
        mode=payload.mode,
        datasourceId=ds.id,
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
    await _ownedSession(db, sessionId, user)
    removed = await _sessionService.deleteSession(db, sessionId)
    if removed == 0:
        # 归属已过仍删 0 行 = 并发下已被另一请求删掉；语义上仍是「不存在」。
        raise NotFoundError("研究会话不存在")
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
    row = await _ownedSession(db, sessionId, user)
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
    row = await _ownedSession(db, sessionId, user)
    turn = await _sessionService.appendTurn(
        db, sessionId=sessionId, role="user", content={"question": payload.question}
    )
    # 先取原始值再 commit（不依赖 expire_on_commit 语义，避免提交后 lazy-load）
    turnId, mode, userId = turn.id, row.mode, user.dbUserId
    await db.commit()
    background.add_task(
        _runTurnInBackground, str(sessionId), str(turnId), payload.question, userId, mode
    )
    return ResearchTurnAccepted(sessionId=sessionId, turnId=turnId, status="running")


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
    await _ownedSession(db, checkpoint.session_id, user)  # 越权同样 404，不泄露存在性
    if checkpoint.status != "pending":
        raise ConflictError(f"检查点已决策（{checkpoint.status}），不可重复提交")
    nextPhaseValue = nextPhase(checkpoint, payload.action)
    background.add_task(
        _resumeTurnInBackground,
        str(checkpoint.id),
        str(checkpoint.session_id),
        payload.action,
        dict(payload.choice),
    )
    return CheckpointAnswerRead(sessionStatus="running", nextPhase=nextPhaseValue)


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
    await _ownedSession(db, sessionId, user)
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
    await _ownedSession(db, sessionId, user)
    reports = await _sessionService.listReports(db, sessionId)
    return [_reportSummary(report) for report in reports]


# ---------------------------------------------------------------------------
# SSE：research.* 事件流（Task 8，独立端点）
# ---------------------------------------------------------------------------


@router.get("/stream", summary="研究进度 SSE（research.* 事件族）")
async def streamSessionEvents(
    sessionId: uuid.UUID,
    user: CurrentUser = Depends(_requireResearchUser),
) -> StreamingResponse:
    """订阅本会话的进度事件（设计 §4.5）。

    **先订阅再触发 turn**：本端点不做历史回放，客户端须在 POST turns / answer 之前
    建流（brief 契约）。鉴权与越权同 REST 口径：他人会话 404（不泄露存在性）。
    归属校验用**短会话**（流是长连接，不能把请求级 session 拖到流结束）。
    """
    async with getSessionFactory()() as db:
        await _ownedSession(db, sessionId, user)
    return StreamingResponse(
        _eventFrames(str(sessionId)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _eventFrames(sessionId: str) -> AsyncIterator[str]:
    """队列 → SSE 帧：首帧 connected；空闲发心跳；终态事件或客户端断连后收尾退订。"""
    queue = _bus.subscribe(sessionId)
    try:
        yield _sseFrame(EVENT_CONNECTED, {"sessionId": sessionId})
        while True:
            try:
                event, payload = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                yield HEARTBEAT_LINE
                continue
            yield _sseFrame(event, payload)
            if _isTerminal(event, payload):
                return
    finally:
        # 客户端断连（生成器被取消）与正常收尾都走这里：不留下悬空订阅
        _bus.unsubscribe(sessionId, queue)


def _sseFrame(event: str, payload: dict[str, Any]) -> str:
    """SSE 帧（`json.dumps` 转义换行，payload 不会破坏帧边界）。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


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
    sessionId: str, turnId: str, question: str, userId: int, mode: str
) -> None:
    """后台跑状态机：自开会话 → `runTurn` → 收尾 commit。

    异常已在状态机内 `research.error` + 会话置 failed；此处只兜底 rollback 与日志
    （后台任务不得把异常抛回响应 —— 那会让 202 变成 500）。

    `emit` 接线（Task 8）：`partial(bus.publish, sessionId)` —— 状态机发的事件即刻
    广播给已订阅的流端（订阅必须先于本任务，流端不做历史回放）。
    """
    service = buildResearchAgentService()
    async with getSessionFactory()() as session:
        try:
            await service.runTurn(
                session,
                sessionId=uuid.UUID(sessionId),
                turnId=uuid.UUID(turnId),
                question=question,
                userId=userId,
                mode=mode,
                emit=partial(_bus.publish, sessionId),
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 —— 后台任务兜底：留痕 + 回滚，不上抛
            logger.exception("研究 turn 后台执行失败: session=%s turn=%s", sessionId, turnId)
            await session.rollback()
            await _markTerminalFailure(service, session, sessionId, str(exc))


async def _resumeTurnInBackground(
    checkpointId: str, sessionId: str, action: str, choice: dict[str, Any]
) -> None:
    """后台按用户决策续跑状态机（resolveCheckpoint 在 `resumeTurn` 内完成）。

    `sessionId` 由调用方显式传入（Task 8）：`emit` 必须绑定会话键才能投到正确的流。
    """
    service = buildResearchAgentService()
    async with getSessionFactory()() as session:
        try:
            await service.resumeTurn(
                session,
                checkpointId=uuid.UUID(checkpointId),
                action=action,
                choice=choice,
                emit=partial(_bus.publish, sessionId),
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 —— 同上：留痕 + 回滚
            logger.exception("研究 turn 恢复失败: checkpoint=%s action=%s", checkpointId, action)
            await session.rollback()
            await _markTerminalFailure(service, session, sessionId, str(exc))


async def _markTerminalFailure(
    service: ResearchAgentService, session: AsyncSession, sessionId: str, message: str
) -> None:
    """后台 wrapper 兜底：补发终态 error（关流）+ 会话落 failed（rollback 后同 session 开新事务）。

    **单一终态路径**：本函数只服务「后台 wrapper 捕获到未预期异常」，code 恒为终态码
    `ERROR_TURN_FAILED`。Task 14 / N4 移除的旧写法 `if code not in TERMINAL_ERROR_CODES:
    return` 因 code 恰是终态字面量而**恒假**——与 docstring「不硬编码字面量」一起说谎，是死分支；
    集合判定只保留在 `_isTerminal`（事件侧决定是否关流）。rollback 后写库失败只留痕、
    不掩盖原始异常（原始异常已由调用方 `logger.exception` 留痕）。
    """
    await _bus.publish(sessionId, EVENT_ERROR, errorPayload(ERROR_TURN_FAILED, message, phase=None))
    try:
        await service.markFailed(session, uuid.UUID(sessionId))
        await session.commit()
    except Exception:  # noqa: BLE001 —— rollback 后写库失败只留痕，不掩盖原始异常
        logger.exception("终态错误落 failed 失败: session=%s", sessionId)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _ownedSession(
    db: AsyncSession, sessionId: uuid.UUID, user: CurrentUser
) -> ResearchSession:
    """按 id 取会话并校验归属；不存在 / 非本人一律 404（不泄露存在性）。"""
    row = await db.get(ResearchSession, sessionId)
    if row is None or row.created_by != user.dbUserId:
        raise NotFoundError("研究会话不存在")
    return row


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
    """`prompt` 从 `options[PROMPT_KEY]` 派生（checkpoint 无 prompt 列，Task 3 裁定 #1）。"""
    options = row.options or {}
    return ResearchCheckpointRead(
        id=row.id,
        phase=row.phase,
        status=row.status,
        options=options,
        prompt=str(options.get(PROMPT_KEY) or ""),
        userChoice=row.user_choice,
        decidedAt=row.decided_at,
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
