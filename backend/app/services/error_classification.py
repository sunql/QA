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
from app.services.messages_zh import (
    MSG_DATASOURCE_CONNECTIVITY,
    MSG_INTERNAL_ERROR,
    MSG_LLM_GENERIC,
    MSG_LLM_PROVIDER_PROTOCOL,
    MSG_LLM_PROVIDER_TRANSIENT,
    MSG_QUERY_TIMEOUT,
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
    # 注：Nl2SqlError 包含重试耗尽（ErrorType.PROVIDER_TRANSIENT 在第 4 步先匹配）
    # 之外的协议级失败（SQL 解析错误、Provider 4xx 重试耗尽后的兜底）。这里把
    # Nl2SqlError 落到 PROVIDER_PROTOCOL 是**重试耗尽**后的兜底分类，不是把
    # "任何 Nl2SqlError 都视为协议问题" — 详见第 4 步 cause chain 优先。
    if isinstance(exc, Nl2SqlError):
        return ErrorType.PROVIDER_PROTOCOL

    # 7. DomainError (其他)
    if isinstance(exc, DomainError):
        return ErrorType.DOMAIN

    # 8. 兜底
    return ErrorType.INTERNAL


# P2-1 review M1：异常 → 用户面向文案映射（前端只看到这些）
# 设计原则：
# - DOMAIN / SCHEMA_REFERENCE / SQL_SYNTAX / PERMISSION：业务级文案，本来就是
#   给用户看的，**保留** exc.message（去掉 str(exc) 套娃层）
# - LLM / PROVIDER_* / INTERNAL / DB_CONNECTIVITY / QUERY_TIMEOUT：底层异常文案
#   可能含 provider 名、status code、stack 信息，**脱敏**为友好文案
#
# 原始 exc.message 进 logger（调用方负责），不在 payload 里露给前端。
_USER_FACING_BY_TYPE: dict[ErrorType, str] = {
    ErrorType.DOMAIN: "domain_passthrough",  # 标记：调用方需用 exc.message
    ErrorType.SCHEMA_REFERENCE: "domain_passthrough",
    ErrorType.SQL_SYNTAX: "domain_passthrough",
    ErrorType.PERMISSION: "domain_passthrough",
    ErrorType.PROVIDER_TRANSIENT: MSG_LLM_PROVIDER_TRANSIENT,
    ErrorType.PROVIDER_PROTOCOL: MSG_LLM_PROVIDER_PROTOCOL,
    ErrorType.LLM: MSG_LLM_GENERIC,
    ErrorType.DB_CONNECTIVITY: MSG_DATASOURCE_CONNECTIVITY,
    ErrorType.QUERY_TIMEOUT: MSG_QUERY_TIMEOUT,
    ErrorType.INTERNAL: MSG_INTERNAL_ERROR,
}

# 走 passthrough 的类型（用 exc.message 而非脱敏文案）
_PASSTHROUGH_TYPES: frozenset[ErrorType] = frozenset({
    ErrorType.DOMAIN,
    ErrorType.SCHEMA_REFERENCE,
    ErrorType.SQL_SYNTAX,
    ErrorType.PERMISSION,
})


def sanitizeErrorMessage(exc: BaseException, errorType: ErrorType | None = None) -> str:
    """异常 → 用户面向文案（脱敏或 passthrough）。

    - errorType=None 时内部调 classifyError() 判定（避免重复扫描）
    - 走 passthrough 的类型用 exc.message（已 user-facing）
    - 其他类型用 _USER_FACING_BY_TYPE 的脱敏文案
    - 原始 exc.message 永远不直接进 SSE error 事件 payload（除非 passthrough）

    调用方约定：原始异常信息应同时落 logger.warning/exception，便于排障。
    """
    if errorType is None:
        errorType = classifyError(exc)
    if errorType in _PASSTHROUGH_TYPES:
        # exc.message 是 DomainError 的 user-facing 字段（不是 str(exc)）
        return getattr(exc, "message", None) or str(exc)
    return _USER_FACING_BY_TYPE[errorType]
