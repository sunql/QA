"""Read-only handler whitelist, tool-schema/argument validation and observation.

Security posture (deny by default):
- Only seven read-only BUILTIN handlers may be exposed to the orchestration
  Planner. ``nl2sql_default``/``execute_sql`` are deliberately absent.
- Tool input schemas must be strict JSON Schema objects: root ``type="object"``,
  a dict ``properties`` and no ``patternProperties`` / truthy or typed
  ``additionalProperties``. Arguments are rejected when they contain keys the
  schema does not declare, recursively for nested objects and array items.
- Validation errors only ever expose field paths and the failing JSON Schema
  keyword, never the submitted argument values.
- Observations are bounded JSON-safe strings; the canonical
  ``app.utils.json_safe.jsonSafe`` is the only normalizer used.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.domain.exceptions import ValidationError
from app.services.agent_tool_types import ToolResult
from app.utils.json_safe import jsonSafe

READ_ONLY_HANDLER_REFS = frozenset({
    "supplier_360",
    "supplier_risk",
    "graph_traverse",
    "wiki_search",
    "wiki_read",
    "rule_evaluate",
    "coverage_status",
})

_OBSERVATION_MARKER = "[truncated]"
_MAX_REPORTED_ERRORS = 10


def validateObjectSchema(schema: Any) -> None:
    """Validate a tool input schema is a safe, strict object schema.

    Raises the domain ``ValidationError`` (422 at the API boundary), never
    ``jsonschema`` exceptions.
    """
    if not isinstance(schema, dict):
        raise ValidationError("tool input schema must be an object")
    if schema.get("type") != "object":
        raise ValidationError("tool input schema root type must be 'object'")
    if not isinstance(schema.get("properties"), dict):
        raise ValidationError("tool input schema properties must be an object")
    if "patternProperties" in schema:
        raise ValidationError("tool input schema must not use patternProperties")
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        raise ValidationError(
            "tool input schema additionalProperties must be false or absent"
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValidationError(
            "tool input schema is invalid",
            detail=_schemaErrorLocation(exc),
        ) from exc


def validateToolArguments(schema: dict, args: Any) -> dict:
    """Validate ``args`` against ``schema`` and return a new dict copy.

    Unknown keys are rejected recursively before full JSON Schema validation so
    the error carries a precise field path and never echoes a value.
    """
    validateObjectSchema(schema)
    if not isinstance(args, dict):
        raise ValidationError("tool arguments must be an object", detail="$")

    unknown = _unknownKeyPaths(schema, args, "")
    if unknown:
        raise ValidationError(
            "tool arguments contain unknown fields",
            detail=", ".join(sorted(unknown)),
        )

    errors = sorted(
        Draft202012Validator(schema).iter_errors(args),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        locations = [
            f"{_pathText(error.absolute_path)} ({error.validator})"
            for error in errors[:_MAX_REPORTED_ERRORS]
        ]
        raise ValidationError(
            "tool arguments do not match schema",
            detail="; ".join(locations),
        )

    return dict(args)


def serializeObservation(result: ToolResult, maxChars: int) -> dict:
    """Return a bounded, JSON-safe observation payload (fresh dict)."""
    safe = jsonSafe({"data": result.data, "answer": result.answer})
    raw = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    text = (
        raw
        if len(raw) <= maxChars
        else raw[: max(0, maxChars - len(_OBSERVATION_MARKER))] + _OBSERVATION_MARKER
    )
    return {"text": text, "truncated": len(raw) > maxChars, "original_chars": len(raw)}


def _schemaErrorLocation(exc: SchemaError) -> str:
    """Describe where a schema is invalid without dumping the schema value."""
    keyword = exc.validator or "schema"
    jsonPath = getattr(exc, "json_path", None) or "$"
    return f"{keyword} at {jsonPath}"


def _pathText(path: Any) -> str:
    parts = [str(part) for part in path]
    return ".".join(parts) if parts else "$"


def _unknownKeyPaths(schema: Any, value: Any, path: str) -> list[str]:
    """Recursively collect argument paths whose object keys are undeclared."""
    if not isinstance(schema, dict):
        return []

    schemaType = schema.get("type")
    if schemaType == "array" and isinstance(value, list):
        items = schema.get("items")
        if not isinstance(items, dict):
            return []
        found: list[str] = []
        for index, item in enumerate(value):
            found.extend(_unknownKeyPaths(items, item, f"{path}[{index}]"))
        return found

    if isinstance(value, dict) and (schemaType == "object" or "properties" in schema):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        found = []
        for key, item in value.items():
            childPath = f"{path}.{key}" if path else str(key)
            childSchema = properties.get(key)
            if key not in properties:
                found.append(childPath)
            elif isinstance(childSchema, dict):
                found.extend(_unknownKeyPaths(childSchema, item, childPath))
        return found

    return []
