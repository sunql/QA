"""classifyError() 11 个错误分类 unit test（P2-1 RED/GREEN）。"""
from __future__ import annotations

import pytest

from app.domain.exceptions import (
    DomainError,
    LlmClientError,
    Nl2SqlError,
    OntologyError,
    PermissionDeniedError,
    SqlSafetyError,
    DataSourceError,
)
from app.services.error_classification import classifyError
from app.services.stream_events import ErrorType


def test_classify_sql_safety_error_to_sql_syntax():
    exc = SqlSafetyError("SELECT * FROM DROP", sql="SELECT * FROM DROP")
    assert classifyError(exc) == ErrorType.SQL_SYNTAX


def test_classify_permission_denied_to_permission():
    exc = PermissionDeniedError("无权访问")
    assert classifyError(exc) == ErrorType.PERMISSION


def test_classify_ontology_error_to_schema_reference():
    exc = OntologyError("class not found")
    assert classifyError(exc) == ErrorType.SCHEMA_REFERENCE


def test_classify_data_source_timeout_to_query_timeout():
    exc = DataSourceError("query timed out after 30s")
    assert classifyError(exc) == ErrorType.QUERY_TIMEOUT


def test_classify_data_source_other_to_db_connectivity():
    exc = DataSourceError("connection refused")
    assert classifyError(exc) == ErrorType.DB_CONNECTIVITY


def test_classify_llm_5xx_cause_to_provider_transient():
    # LlmClientError 包了 status_code=503 的 __cause__
    cause = Exception("service unavailable")
    cause.status_code = 503
    exc = LlmClientError("call failed")
    exc.__cause__ = cause
    assert classifyError(exc) == ErrorType.PROVIDER_TRANSIENT


def test_classify_llm_4xx_cause_to_provider_protocol():
    cause = Exception("unauthorized")
    cause.status_code = 401
    exc = LlmClientError("call failed")
    exc.__cause__ = cause
    assert classifyError(exc) == ErrorType.PROVIDER_PROTOCOL


def test_classify_llm_timeout_cause_to_provider_transient():
    cause = Exception("connection timed out")
    exc = LlmClientError("call failed")
    exc.__cause__ = cause
    assert classifyError(exc) == ErrorType.PROVIDER_TRANSIENT


def test_classify_llm_429_to_provider_transient():
    """429 (rate limit) is retryable -> PROVIDER_TRANSIENT."""
    cause = Exception("rate limit exceeded")
    cause.status_code = 429
    exc = LlmClientError("call failed")
    exc.__cause__ = cause
    assert classifyError(exc) == ErrorType.PROVIDER_TRANSIENT


def test_classify_llm_fallback_to_llm():
    """LlmClientError without clear cause chain -> LLM (backward compat)."""
    exc = LlmClientError("unknown failure")
    assert classifyError(exc) == ErrorType.LLM


def test_classify_nl2sql_error_to_provider_protocol():
    exc = Nl2SqlError("retries exhausted")
    assert classifyError(exc) == ErrorType.PROVIDER_PROTOCOL


def test_classify_generic_domain_error_to_domain():
    class CustomDomainError(DomainError):
        pass
    exc = CustomDomainError("custom")
    assert classifyError(exc) == ErrorType.DOMAIN


def test_classify_unknown_exception_to_internal():
    assert classifyError(ValueError("???")) == ErrorType.INTERNAL
