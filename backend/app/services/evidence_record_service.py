"""SQL 执行自动 Evidence 记录（v3.1 任务 B2，蓝图 §5.7 Evidence Agent 降级版）。

收口点在 `business_db_pool` 的 `execute_read_only`（三 adapter 统一经装饰器
`_recordEvidenceAfterSuccess` 进入本模块）：查询成功后同步计算 result_hash
（主链路唯一新增成本），落库走 `asyncio.create_task` 后台任务——不阻塞查询
主链路（查询延迟红线），失败仅 logging.warning，绝不让查询失败（best-effort
降级原则，与 Oracle ALTER SESSION 同款）。

ContextVar：chat session_id 与调用方 user_id 均由 ChatService 两个入口
（processMessage / processMessageStream）设置，默认 None——非 chat 调用方
（DQ evaluator / introspection 等）不阻塞，落库行 session_id 为 NULL。
user_id（R2 必修 2，security H2）供 chat 消息落库打归属标
（session_message.user_id），是 /evidences 归属守卫的数据源：服务端 actor
派生，不从客户端读。

Blueprint §5.7：SQL_QUERY 型证据创建时不挂 claim（claim_id NULL），claim
关联由上层（B3/B4）事后填充；表约束由 0098 迁移放开 NOT NULL。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from app.domain.wiki_models import Evidence
from app.infrastructure.database import getSessionFactory

logger = logging.getLogger(__name__)

SOURCE_TYPE_SQL_QUERY = "SQL_QUERY"
SOURCE_TYPE_METRIC_RESULT = "METRIC_RESULT"

# 后台落库任务的模块级强引用（create_task 弱引用会被 GC，需持握防丢失；
# 完成即由 done_callback 回收——ontology_service._PENDING_SYNC_TASKS 同款）
_PENDING_EVIDENCE_TASKS: set[asyncio.Task] = set()

_currentChatSessionId: ContextVar[str | None] = ContextVar(
    "current_chat_session_id", default=None
)


def setChatSessionId(sessionId: str | None) -> Token:
    """设置当前请求链路的 chat session_id（ChatService 入口调用）。"""
    return _currentChatSessionId.set(sessionId)


def resetChatSessionId(token: Token) -> None:
    """复位 contextvar（入口 finally 调用，避免向任务上下文残留泄漏）。"""
    _currentChatSessionId.reset(token)


def currentChatSessionId() -> str | None:
    """读取当前 chat session_id；非 chat 链路返回 None。"""
    return _currentChatSessionId.get()


_currentChatUserId: ContextVar[str | None] = ContextVar(
    "current_chat_user_id", default=None
)


def setChatUserId(userId: str | None) -> Token:
    """标记当前 chat 请求的调用方（服务端 actor，随 user 参数传入，非客户端自报）。"""
    return _currentChatUserId.set(userId)


def resetChatUserId(token: Token) -> None:
    """复位 user contextvar（与 setChatSessionId 同一入口 finally 成对调用）。"""
    _currentChatUserId.reset(token)


def currentChatUserId() -> str | None:
    """读取当前调用方；未传 user（测试直调等）返回 None（行不打标）。"""
    return _currentChatUserId.get()


def computeResultHash(rows: list[dict[str, Any]]) -> str:
    """结果集指纹，口径固定为 brief 原文：sha256(json.dumps(rows, sort_keys=True, default=str))。

    改口径 = 历史证据 hash 全部失配，调用方不得自行加参数。
    """
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode()
    ).hexdigest()


def buildSqlEvidencePayload(
    *,
    sql: str,
    resultHash: str,
    rowCount: int,
    executionTimeMs: int,
    datasourceId: int | None,
) -> dict[str, Any]:
    """构造 SQL_QUERY 型 payload（六字段契约，见 plan-person-b.md B1/B2）。"""
    return {
        "sql": sql,
        "params": {},
        "result_hash": resultHash,
        "row_count": rowCount,
        "execution_time_ms": executionTimeMs,
        "datasource_id": datasourceId,
    }


async def persistSqlQueryEvidence(
    *, payload: dict[str, Any], sessionId: str | None
) -> None:
    """落一条 SQL_QUERY 型 evidence（独立会话独立事务，由后台任务调用）。

    claim_id 恒为 NULL：蓝图 §5.7 关联由上层填充；本函数不猜 claim。
    """
    factory = getSessionFactory()
    async with factory() as session:
        session.add(
            Evidence(
                claim_id=None,
                source_type=SOURCE_TYPE_SQL_QUERY,
                payload=payload,
                session_id=sessionId,
            )
        )
        await session.commit()


def scheduleSqlQueryEvidence(
    *,
    sql: str,
    rows: list[dict[str, Any]],
    startedAt: float,
    datasourceId: int | None,
) -> None:
    """查询成功后调用（同步、非阻塞）：算 hash → 调度后台落库。

    在查询调用方的 context 里同步执行：session_id 在此刻读取并随参数传入
    后台任务，规避异步链路的 context 传播微妙性。任何异常（含 hash 计算失败）
    只 warning，查询结果不受影响。
    """
    try:
        payload = buildSqlEvidencePayload(
            sql=sql,
            resultHash=computeResultHash(rows),
            rowCount=len(rows),
            executionTimeMs=max(0, int((time.monotonic() - startedAt) * 1000)),
            datasourceId=datasourceId,
        )
        sessionId = currentChatSessionId()
        task = asyncio.create_task(
            _persistBestEffort(payload=payload, sessionId=sessionId)
        )
        _PENDING_EVIDENCE_TASKS.add(task)
        task.add_done_callback(_PENDING_EVIDENCE_TASKS.discard)
    except Exception as exc:  # noqa: BLE001 - 记录永远不影响查询主链路
        logger.warning("SQL evidence 调度失败（查询结果不受影响）: %s", exc)


async def _persistBestEffort(
    *,
    payload: dict[str, Any],
    sessionId: str | None,
    persist: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> None:
    """后台落库包装：持久化失败仅 warning（best-effort，绝不上抛）。

    persist 缺省走 SQL_QUERY（B2 语义不变）；METRIC_RESULT（MR）传入
    persistMetricResultEvidence 复用同一 best-effort 包装。
    """
    persistFn = persist if persist is not None else persistSqlQueryEvidence
    try:
        await persistFn(payload=payload, sessionId=sessionId)
    except Exception as exc:  # noqa: BLE001 - best-effort 降级路径
        logger.warning(
            "evidence 落库失败 session_id=%s sql=%.120s: %s",
            sessionId,
            payload.get("sql", ""),
            exc,
        )


# =========================================================================
# v3.1 任务 MR：METRIC_RESULT 自动落库（蓝图 §4.12，补 B1 后半）
# =========================================================================


def extractSampleValue(rows: list[dict[str, Any]]) -> float | str | None:
    """提取结果首行第一个数值单元格（蓝图 §4.12 sample_value）。

    口径：int/float 直取（bool 是 int 子类，跳过）；Decimal 先 str 再 float
    （JSONB 序列化边界）；数值字符串可转则取。无数值 / 空行 / 任何异常
    → None，绝不抛（best-effort 提取）。
    """
    try:
        if not rows:
            return None
        for value in rows[0].values():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, Decimal):
                return float(str(value))
            if isinstance(value, str):
                try:
                    return float(value.strip())
                except ValueError:
                    continue
        return None
    except Exception:  # noqa: BLE001 - 提取永远不影响主链路
        return None


def buildMetricResultPayload(
    *,
    metricCode: str,
    metricName: str | None,
    confidence: float,
    period: str | None,
    sampleValue: float | str | None,
    rowCount: int,
    resultHash: str,
    calcTime: str,
    datasourceId: int | None,
) -> dict[str, Any]:
    """构造 METRIC_RESULT 型 payload（蓝图 §4.12 四要素 + 追溯上下文）。"""
    return {
        "metric_code": metricCode,
        "metric_name": metricName,
        "confidence": confidence,
        "period": period,
        "sample_value": sampleValue,
        "row_count": rowCount,
        "result_hash": resultHash,
        "calc_time": calcTime,
        "datasource_id": datasourceId,
    }


async def persistMetricResultEvidence(
    *, payload: dict[str, Any], sessionId: str | None
) -> None:
    """落一条 METRIC_RESULT 型 evidence（独立会话独立事务，由后台任务调用）。

    claim_id 恒为 NULL：蓝图 §5.7 关联由上层填充；本函数不猜 claim。
    """
    factory = getSessionFactory()
    async with factory() as session:
        session.add(
            Evidence(
                claim_id=None,
                source_type=SOURCE_TYPE_METRIC_RESULT,
                payload=payload,
                session_id=sessionId,
            )
        )
        await session.commit()


def scheduleMetricResultEvidence(
    *,
    metricCode: str,
    metricName: str | None,
    confidence: float,
    rows: list[dict[str, Any]],
    datasourceId: int | None,
    period: str | None = None,
) -> None:
    """L1 KPI 命中且查询成功执行后调用（同步、非阻塞）。

    逐行镜像 scheduleSqlQueryEvidence 的 best-effort 语义：在调用方 context
    里同步构造 payload（sample_value / hash / calc_time），session_id 在此刻
    读取并随参数传入后台任务；任何异常只 warning，查询结果不受影响。
    period best-effort：执行时上下文有则填，无则 None——不猜。
    """
    try:
        payload = buildMetricResultPayload(
            metricCode=metricCode,
            metricName=metricName,
            confidence=confidence,
            period=period,
            sampleValue=extractSampleValue(rows),
            rowCount=len(rows),
            resultHash=computeResultHash(rows),
            calcTime=datetime.now(timezone.utc).isoformat(),
            datasourceId=datasourceId,
        )
        sessionId = currentChatSessionId()
        task = asyncio.create_task(
            _persistBestEffort(
                payload=payload,
                sessionId=sessionId,
                persist=persistMetricResultEvidence,
            )
        )
        _PENDING_EVIDENCE_TASKS.add(task)
        task.add_done_callback(_PENDING_EVIDENCE_TASKS.discard)
    except Exception as exc:  # noqa: BLE001 - 记录永远不影响查询主链路
        logger.warning("METRIC evidence 调度失败（查询结果不受影响）: %s", exc)
