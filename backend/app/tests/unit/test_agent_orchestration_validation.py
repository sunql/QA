"""Task 2 read-only whitelist, schema/argument validation and observation tests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.domain.exceptions import ValidationError
from app.services.agent_orchestration_validation import (
    READ_ONLY_HANDLER_REFS,
    serializeObservation,
    validateObjectSchema,
    validateToolArguments,
)
from app.services.agent_tool_types import ToolResult


def test_only_seven_read_only_handlers_are_allowed():
    assert isinstance(READ_ONLY_HANDLER_REFS, frozenset)
    assert frozenset({
        "supplier_360", "supplier_risk", "graph_traverse", "wiki_search",
        "wiki_read", "rule_evaluate", "coverage_status",
    }) == READ_ONLY_HANDLER_REFS
    assert "nl2sql_default" not in READ_ONLY_HANDLER_REFS
    assert "execute_sql" not in READ_ONLY_HANDLER_REFS


def test_unknown_arguments_are_rejected_before_handler():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    validateObjectSchema(schema)
    validateToolArguments(schema, {"query": "risk"})
    with pytest.raises(ValidationError):
        validateToolArguments(schema, {"query": "risk", "handlerRef": "execute_sql"})


def test_observation_is_bounded_and_json_safe():
    result = ToolResult(data={"text": "abcdef"}, answer="answer")
    observed = serializeObservation(result, 24)
    assert observed["truncated"] is True
    assert observed["text"].endswith("[truncated]")
    assert len(observed["text"]) <= 24
    assert observed["original_chars"] > 24


def test_observation_returns_fresh_dict_and_reports_exact_shape_when_not_truncated():
    result = ToolResult(data={"text": "ok"}, answer="answer")
    first = serializeObservation(result, 1000)
    second = serializeObservation(result, 1000)
    assert first == second
    assert first is not second
    assert first["truncated"] is False
    assert first["original_chars"] == len(first["text"])
    assert first["text"] == '{"data":{"text":"ok"},"answer":"answer"}'


def test_observation_normalizes_non_json_values_with_json_safe():
    result = ToolResult(
        data={"amount": Decimal("12.5"), "at": datetime(2026, 10, 26, 1, 2, 3)},
        answer="a",
    )
    observed = serializeObservation(result, 1000)
    assert '"amount":12.5' in observed["text"]
    assert '"at":"2026-10-26T01:02:03"' in observed["text"]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "properties": {}},
        {"type": "object"},  # missing properties
        {"type": "object", "properties": []},  # properties must be a dict
        {"type": "object", "properties": {}, "patternProperties": {"^x": {"type": "string"}}},
        {"type": "object", "properties": {}, "additionalProperties": True},
        {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}},
        {"type": "object", "properties": {"q": {"type": 42}}},  # invalid JSON Schema
        "not-a-schema",
        None,
    ],
)
def test_malformed_schemas_fail_before_any_handler(schema):
    with pytest.raises(ValidationError):
        validateObjectSchema(schema)


@pytest.mark.parametrize(
    "args",
    [
        None,
        [],
        "query",
        42,
    ],
)
def test_non_dict_arguments_are_rejected(args):
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    with pytest.raises(ValidationError):
        validateToolArguments(schema, args)


def test_nested_unknown_keys_are_rejected_recursively():
    schema = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "properties": {"year": {"type": "integer"}},
            },
        },
    }
    validateToolArguments(schema, {"filter": {"year": 2025}})
    with pytest.raises(ValidationError) as exc_info:
        validateToolArguments(schema, {"filter": {"year": 2025, "dropTable": "x"}})
    assert "filter.dropTable" in str(exc_info.value) or "filter.dropTable" in (
        exc_info.value.detail or ""
    )


def test_unknown_keys_inside_array_items_are_rejected():
    schema = {
        "type": "object",
        "properties": {
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"year": {"type": "integer"}},
                },
            },
        },
    }
    validateToolArguments(schema, {"filters": [{"year": 2025}]})
    with pytest.raises(ValidationError):
        validateToolArguments(schema, {"filters": [{"year": 2025, "rawSql": "select 1"}]})


def test_schema_violations_report_paths_without_echoing_argument_values():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    secret = "do-not-echo-this-value"
    with pytest.raises(ValidationError) as exc_info:
        validateToolArguments(schema, {"query": 12345})
    rendered = f"{exc_info.value.message} {exc_info.value.detail or ''}"
    assert "query" in rendered
    assert "12345" not in rendered
    assert secret not in rendered


def test_validation_returns_a_new_dict_copy():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    args = {"query": "risk"}
    validated = validateToolArguments(schema, args)
    assert validated == args
    assert validated is not args
    validated["query"] = "changed"
    assert args["query"] == "risk"


def test_optional_and_additional_properties_false_schemas_are_accepted():
    validateObjectSchema({
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "additionalProperties": False,
        "required": ["query"],
    })
    with pytest.raises(ValidationError):
        validateToolArguments(
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            {},
        )
