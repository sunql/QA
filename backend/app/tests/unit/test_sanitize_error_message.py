"""P2-1 review M1 落地：sanitizeErrorMessage 脱敏。

- LLM / PROVIDER_* / INTERNAL / DB_CONNECTIVITY / QUERY_TIMEOUT → 脱敏文案
- DOMAIN / SCHEMA_REFERENCE / SQL_SYNTAX / PERMISSION → passthrough（业务文案本来就是 user-facing）
- 原始 exc.message 永不进 SSE error / HTTP response 顶层（除非 passthrough）

设计口径：
- sanitizeErrorMessage(exc, errorType) 是纯函数（不读 DB / 不打日志）
- 调用方负责把 exc.message 写日志 + 落库（排障用）
"""
from __future__ import annotations

import pytest

from app.domain.exceptions import (
    DataSourceError,
    DomainError,
    LlmClientError,
    Nl2SqlError,
    OntologyError,
    PermissionDeniedError,
    SqlSafetyError,
    ValidationError,
)
from app.services.error_classification import (
    classifyError,
    sanitizeErrorMessage,
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


# =============================================================================
# Passthrough 类型（业务文案本来就是 user-facing）
# =============================================================================


def test_sanitize_domain_passthrough_uses_exc_message():
    """DomainError (custom) → DOMAIN passthrough，使用 exc.message。"""
    exc = DomainError("请提供问题")

    msg = sanitizeErrorMessage(exc, ErrorType.DOMAIN)

    assert msg == "请提供问题"


def test_sanitize_validation_passthrough():
    """ValidationError → DOMAIN passthrough。"""
    exc = ValidationError("问题不能为空")

    msg = sanitizeErrorMessage(exc, ErrorType.DOMAIN)

    assert msg == "问题不能为空"


def test_sanitize_sql_safety_passthrough():
    """SqlSafetyError → SQL_SYNTAX passthrough。"""
    exc = SqlSafetyError("检测到非只读 SQL")

    msg = sanitizeErrorMessage(exc, ErrorType.SQL_SYNTAX)

    assert msg == "检测到非只读 SQL"


def test_sanitize_permission_passthrough():
    """PermissionDeniedError → PERMISSION passthrough。"""
    exc = PermissionDeniedError("无权访问该数据源")

    msg = sanitizeErrorMessage(exc, ErrorType.PERMISSION)

    assert msg == "无权访问该数据源"


def test_sanitize_ontology_passthrough():
    """OntologyError → SCHEMA_REFERENCE passthrough。"""
    exc = OntologyError("类 X 不存在")

    msg = sanitizeErrorMessage(exc, ErrorType.SCHEMA_REFERENCE)

    assert msg == "类 X 不存在"


# =============================================================================
# 脱敏类型（provider/LLM/底层异常 → 友好文案）
# =============================================================================


def test_sanitize_llm_client_500_to_provider_transient():
    """LlmClientError 5xx → PROVIDER_TRANSIENT 脱敏文案。"""
    cause = Exception("upstream 502 Bad Gateway")
    cause.status_code = 502
    exc = LlmClientError("调用 qwen 失败: upstream 502 Bad Gateway", provider="qwen")
    exc.__cause__ = cause

    msg = sanitizeErrorMessage(exc, ErrorType.PROVIDER_TRANSIENT)

    assert msg == MSG_LLM_PROVIDER_TRANSIENT
    # 反向断言：provider 名 / status code 都不在文案里
    assert "qwen" not in msg
    assert "502" not in msg
    assert "upstream" not in msg


def test_sanitize_llm_client_400_to_provider_protocol():
    """LlmClientError 4xx → PROVIDER_PROTOCOL 脱敏文案。"""
    cause = Exception("Bad Request")
    cause.status_code = 400
    exc = LlmClientError("调用 openai 失败: Bad Request - invalid api key", provider="openai")
    exc.__cause__ = cause

    msg = sanitizeErrorMessage(exc, ErrorType.PROVIDER_PROTOCOL)

    assert msg == MSG_LLM_PROVIDER_PROTOCOL
    assert "openai" not in msg
    assert "api key" not in msg
    assert "400" not in msg


def test_sanitize_llm_client_generic_to_llm():
    """LlmClientError 无 cause → 兜底 LLM 脱敏文案。"""
    exc = LlmClientError("未知 LLM 失败", provider="qwen")

    msg = sanitizeErrorMessage(exc, ErrorType.LLM)

    assert msg == MSG_LLM_GENERIC
    assert "qwen" not in msg


def test_sanitize_nl2sql_retries_exhausted_to_provider_protocol():
    """Nl2SqlError 重试耗尽 → PROVIDER_PROTOCOL 脱敏。"""
    exc = Nl2SqlError("retries exhausted: failed after 3 attempts with timeout")

    msg = sanitizeErrorMessage(exc, ErrorType.PROVIDER_PROTOCOL)

    assert msg == MSG_LLM_PROVIDER_PROTOCOL
    assert "timeout" not in msg
    assert "3 attempts" not in msg


def test_sanitize_datasource_timeout_to_query_timeout():
    """DataSourceError 含 timeout hint → QUERY_TIMEOUT 脱敏。"""
    exc = DataSourceError("query timed out after 30s")

    msg = sanitizeErrorMessage(exc, ErrorType.QUERY_TIMEOUT)

    assert msg == MSG_QUERY_TIMEOUT
    assert "30s" not in msg


def test_sanitize_datasource_connection_to_connectivity():
    """DataSourceError 通用 → DB_CONNECTIVITY 脱敏。"""
    exc = DataSourceError("connection refused by postgres:5432")

    msg = sanitizeErrorMessage(exc, ErrorType.DB_CONNECTIVITY)

    assert msg == MSG_DATASOURCE_CONNECTIVITY
    assert "postgres" not in msg
    assert "5432" not in msg


def test_sanitize_unknown_exception_to_internal():
    """未预期 Exception → INTERNAL 脱敏。"""
    exc = ValueError("???")

    msg = sanitizeErrorMessage(exc, ErrorType.INTERNAL)

    assert msg == MSG_INTERNAL_ERROR


# =============================================================================
# auto-classify 路径（errorType=None）
# =============================================================================


def test_sanitize_auto_classify_passes_for_llm():
    """errorType=None 时内部调 classifyError 判定。"""
    cause = Exception("rate limit")
    cause.status_code = 429
    exc = LlmClientError("rate limited")
    exc.__cause__ = cause

    msg = sanitizeErrorMessage(exc)  # 不传 errorType

    # 429 → PROVIDER_TRANSIENT → 脱敏文案
    assert msg == MSG_LLM_PROVIDER_TRANSIENT


def test_sanitize_auto_classify_passes_for_domain():
    """errorType=None + DomainError → passthrough。"""
    exc = ValidationError("参数错误")

    msg = sanitizeErrorMessage(exc)

    assert msg == "参数错误"


# =============================================================================
# 反向断言（核心）
# =============================================================================


@pytest.mark.parametrize(
    "exc,errorType",
    [
        # provider 名/状态码敏感信息不应穿透
        (LlmClientError("qwen API call failed: 401 Unauthorized", provider="qwen"), ErrorType.LLM),
        (LlmClientError("azure openai: 503 Service Unavailable", provider="azure"), ErrorType.PROVIDER_TRANSIENT),
        (LlmClientError("openai: invalid api key", provider="openai"), ErrorType.PROVIDER_PROTOCOL),
        (Nl2SqlError("timeout connecting to dw: 30s elapsed"), ErrorType.PROVIDER_PROTOCOL),
        (DataSourceError("postgres connection refused: 127.0.0.1:5432"), ErrorType.DB_CONNECTIVITY),
    ],
)
def test_sanitize_does_not_leak_provider_info(exc: BaseException, errorType: ErrorType):
    """关键反向断言：provider / status_code / endpoint 永远不进 user-facing 文案。"""
    msg = sanitizeErrorMessage(exc, errorType)

    forbidden_substrings = [
        "qwen", "openai", "azure", "postgres", "127.0.0.1", "5432",
        "401", "503", "30s", "elapsed", "invalid", "unavailable", "unauthorized",
    ]
    for forbidden in forbidden_substrings:
        if forbidden.lower() in str(exc).lower():
            # 原 exc.message 含此词时，必须在脱敏后消失
            assert forbidden.lower() not in msg.lower(), (
                f"sanitize 漏了敏感信息 {forbidden!r}: {msg!r} (来源 {exc!r})"
            )
