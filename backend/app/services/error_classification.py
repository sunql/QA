"""异常 → ErrorType 分类（purely informational；retry 决策仍走 isRetryableLlmError）。

P2-1：把 chat_stream.py:253/261 2 处硬编码 ErrorType 替换为自动分类，
前端 error 事件拿到细粒度 errorType 可差异化降级（"权限不足"显示申请按钮，
"连接超时"显示重连按钮，等等）。

判定规则（按 priority 顺序）：
1. isinstance 检查（最精确）
2. __cause__ chain walking（per qa-system-llm-error-cause-chain.md）
3. 字符串 hint 探测（"timeout" / "timed out" / "permission denied"）
4. 兜底 INTERNAL
"""
from __future__ import annotations

from typing import Iterable

from app.domain.exceptions import (
    DataSourceError,
    DomainError,
    LlmClientError,
    Nl2SqlError,
    OntologyError,
    PermissionDeniedError,
    SqlSafetyError,
)
from app.services.stream_events import ErrorType

_TIMEOUT_HINTS = ("timeout", "timed out", "deadline exceeded")
_PERMISSION_HINTS = ("permission", "denied", "forbidden", "unauthorized")


def _walkCauseChain(exc: BaseException) -> Iterable[BaseException]:
    """Walk the entire __cause__ chain (per qa-system-llm-error-cause-chain.md).

    连接/超时类 cause 不带 status_code；判定须走整条链。
    """
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__


def classifyError(exc: BaseException) -> ErrorType:
    """异常 → ErrorType 分类。

    判定顺序：
    1. SqlSafetyError → SQL_SYNTAX
    2. PermissionDeniedError → PERMISSION
    3. OntologyError → SCHEMA_REFERENCE
    4. DataSourceError → QUERY_TIMEOUT (含 timeout hint) | DB_CONNECTIVITY (其他)
    5. LlmClientError → 走 __cause__ chain：
       - 4xx 永久错误（401/403/400）→ PROVIDER_PROTOCOL
       - 5xx / timeout / connection → PROVIDER_TRANSIENT
       - 其他 → LLM (兜底)
    6. Nl2SqlError → PROVIDER_PROTOCOL (重试耗尽 = 协议级失败)
    7. DomainError → DOMAIN
    8. 兜底 INTERNAL
    """
    # 1. SqlSafetyError
    if isinstance(exc, SqlSafetyError):
        return ErrorType.SQL_SYNTAX

    # 2. PermissionDeniedError
    if isinstance(exc, PermissionDeniedError):
        return ErrorType.PERMISSION

    # 3. OntologyError
    if isinstance(exc, OntologyError):
        return ErrorType.SCHEMA_REFERENCE

    # 4. DataSourceError (timeout vs connectivity)
    if isinstance(exc, DataSourceError):
        msg = str(exc).lower()
        if any(hint in msg for hint in _TIMEOUT_HINTS):
            return ErrorType.QUERY_TIMEOUT
        return ErrorType.DB_CONNECTIVITY

    # 5. LlmClientError (走 cause chain 找 status_code)
    if isinstance(exc, LlmClientError):
        for cause in _walkCauseChain(exc):
            status = getattr(cause, "status_code", None) or getattr(cause, "status", None)
            if status is not None:
                try:
                    code = int(status)
                except (TypeError, ValueError):
                    code = 0
                if 400 <= code < 500:
                    if code == 429:
                        return ErrorType.PROVIDER_TRANSIENT
                    return ErrorType.PROVIDER_PROTOCOL
                if 500 <= code < 600:
                    return ErrorType.PROVIDER_TRANSIENT
            cause_msg = str(cause).lower()
            if any(hint in cause_msg for hint in _TIMEOUT_HINTS):
                return ErrorType.PROVIDER_TRANSIENT
        # 兜底：原 ErrorType.LLM（保持向后兼容，error 事件 payload 不破）
        return ErrorType.LLM

    # 6. Nl2SqlError
    if isinstance(exc, Nl2SqlError):
        return ErrorType.PROVIDER_PROTOCOL

    # 7. DomainError (其他)
    if isinstance(exc, DomainError):
        return ErrorType.DOMAIN

    # 8. 兜底
    return ErrorType.INTERNAL
