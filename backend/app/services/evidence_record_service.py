"""SQL 执行自动 Evidence 记录（v3.1 任务 B2，蓝图 §5.7 Evidence Agent 降级版）。

收口点在 `business_db_pool` 的 `execute_read_only`（三 adapter 统一经装饰器
`_recordEvidenceAfterSuccess` 进入本模块）：查询成功后同步计算 result_hash
（主链路唯一新增成本），落库走 `asyncio.create_task` 后台任务——不阻塞查询
主链路（查询延迟红线），失败仅 logging.warning，绝不让查询失败（best-effort
降级原则，与 Oracle ALTER SESSION 同款）。

ContextVar：chat session_id 由 ChatService 两个入口（processMessage /
processMessageStream）设置，默认 None——非 chat 调用方（DQ evaluator /
introspection 等）不阻塞，落库行 session_id 为 NULL。

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
from typing import Any

from app.domain.wiki_models import Evidence
from app.infrastructure.database import getSessionFactory

logger = logging.getLogger(__name__)

SOURCE_TYPE_SQL_QUERY = "SQL_QUERY"

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
    *, payload: dict[str, Any], sessionId: str | None
) -> None:
    """后台落库包装：持久化失败仅 warning（best-effort，绝不上抛）。"""
    try:
        await persistSqlQueryEvidence(payload=payload, sessionId=sessionId)
    except Exception as exc:  # noqa: BLE001 - best-effort 降级路径
        logger.warning(
            "SQL evidence 落库失败 session_id=%s sql=%.120s: %s",
            sessionId,
            payload.get("sql", ""),
            exc,
        )
