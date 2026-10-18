"""Reverse-guard: chat_stream meta frame intent values must remain exact wire strings.

Pre-existing defect per test_stream_meta_single_frame.py:12-13 — "KPI" / "AGENT_RUN"
are NOT IntentType enum values (which are lowercase "query"/"agent_run"). They are
SSE wire format consumed by frontend chatApi/researchStore. Lock the constants to
their exact legacy values so future refactors don't silently break the wire contract.

Per CLAUDE.md immutability rule: always create new objects, never mutate. These
constants are deliberately immutable strings (UPPER_SNAKE_CASE convention for module
constants in this codebase).
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services.chat_constants import (
    META_INTENT_L1,
    META_INTENT_L4,
    ROUTING_LAYER_L1,
    ROUTING_LAYER_L4,
)

# ----------------------------------------------------------------------
# Layer 1: constants exist and have the exact legacy wire values
# ----------------------------------------------------------------------


def test_meta_intent_l1_is_legacy_uppercase_kpi() -> None:
    """Wire contract: L1 meta frame intent must be 'KPI' (NOT 'kpi' / 'query')."""
    assert META_INTENT_L1 == "KPI", (
        f"META_INTENT_L1 changed from wire legacy 'KPI' to {META_INTENT_L1!r}. "
        f"Frontend chatApi depends on exact string. Coordinate before changing."
    )


def test_meta_intent_l4_is_legacy_uppercase_agent_run() -> None:
    """Wire contract: L4 meta frame intent must be 'AGENT_RUN' (NOT 'agent_run')."""
    assert META_INTENT_L4 == "AGENT_RUN", (
        f"META_INTENT_L4 changed from wire legacy 'AGENT_RUN' to {META_INTENT_L4!r}. "
        f"Frontend chatApi depends on exact string. Coordinate before changing."
    )


def test_routing_layer_l1_unchanged() -> None:
    """Wire contract: routingLayer='L1' (uppercase, NOT lowercase 'l1')."""
    assert ROUTING_LAYER_L1 == "L1"


def test_routing_layer_l4_unchanged() -> None:
    """Wire contract: routingLayer='L4' (uppercase, NOT lowercase 'l4')."""
    assert ROUTING_LAYER_L4 == "L4"


def test_meta_intent_l1_not_in_intent_type_enum() -> None:
    """Document pre-existing asymmetry: 'KPI' is NOT an IntentType value.

    IntentType has 13 values, all lowercase ("query", "agent_run", etc.). The
    meta frame's "intent" field uses a DIFFERENT vocabulary — wire format only.
    This test documents the asymmetry so a future refactor doesn't assume
    META_INTENT_L1 should be replaced with IntentType.something.value.
    """
    from app.domain.enums import IntentType

    intent_values = {it.value for it in IntentType}
    assert META_INTENT_L1 not in intent_values, (
        f"META_INTENT_L1={META_INTENT_L1!r} collides with IntentType. "
        f"This is a wire-format constant, not an enum value."
    )
    assert META_INTENT_L4 not in intent_values, (
        f"META_INTENT_L4={META_INTENT_L4!r} collides with IntentType. "
        f"This is a wire-format constant, not an enum value."
    )


# ----------------------------------------------------------------------
# Layer 2: chat_stream.py uses the constants (no raw strings left)
# ----------------------------------------------------------------------


_CHAT_STREAM_PATH = Path(__file__).resolve().parents[2] / "services" / "chat_stream.py"
_CHAT_STREAM_ENTRY_PATH = Path(__file__).resolve().parents[2] / "services" / "chat_stream_entry.py"


def _read_chat_stream_source() -> str:
    """Read chat_stream_entry.py source for static-assertion tests.

    Avoids importing the module (which has heavy side effects: DB session,
    LLM clients). The wire contract is encoded in the literal text.

    Post Phase 1.2 split (PR #54 follow-up): the meta frame ternary that
    was in chat_stream.py moved to chat_stream_entry.py (where the entry
    StreamEntryMixin now constructs the meta frame). This test still
    asserts the same constants are referenced verbatim.
    """
    return _CHAT_STREAM_ENTRY_PATH.read_text(encoding="utf-8")


def test_chat_stream_uses_meta_intent_constants_for_l1() -> None:
    """chat_stream meta frame L1 path uses META_INTENT_L1 (not raw "KPI")."""
    source = _read_chat_stream_source()
    # Find the line that constructs the meta frame's intent field.
    # Per plan: pre-refactor used raw "KPI" / "AGENT_RUN". Post-refactor uses constants.
    intent_line_match = re.search(
        r'^\s*"intent":\s*([^,\n]+),',
        source,
        re.MULTILINE,
    )
    assert intent_line_match is not None, (
        "Could not find meta frame 'intent' line in chat_stream.py"
    )
    intent_expr = intent_line_match.group(1)
    # The expression should reference the constant IDENTIFIER (e.g. META_INTENT_L1),
    # not the bare string value ("KPI"). We assert the identifier is present.
    assert "META_INTENT_L1" in intent_expr, (
        f"meta frame 'intent' line should reference META_INTENT_L1 identifier. "
        f"Got: {intent_expr!r}"
    )
    assert "META_INTENT_L4" in intent_expr, (
        f"meta frame 'intent' line should reference META_INTENT_L4 identifier. "
        f"Got: {intent_expr!r}"
    )


def test_chat_stream_uses_meta_intent_l1_constant_in_ternary() -> None:
    """L1 branch of meta frame ternary references META_INTENT_L1 identifier.

    Split out from the combined test for clearer failure isolation: if a future
    refactor replaces META_INTENT_L1 with raw "KPI" in the L1 branch only, this
    test fails with a precise message rather than co-failing with the L4 branch.
    """
    source = _read_chat_stream_source()
    intent_line_match = re.search(
        r'^\s*"intent":\s*([^,\n]+),',
        source,
        re.MULTILINE,
    )
    assert intent_line_match is not None, (
        "Could not find meta frame 'intent' line in chat_stream.py"
    )
    intent_expr = intent_line_match.group(1)
    assert "META_INTENT_L1" in intent_expr, (
        f"L1 branch of meta frame ternary should use META_INTENT_L1 identifier. "
        f"Got: {intent_expr!r}"
    )


def test_chat_stream_uses_meta_intent_l4_constant_in_ternary() -> None:
    """L4 (else) branch of meta frame ternary references META_INTENT_L4 identifier.

    Split out from the combined test for clearer failure isolation.
    """
    source = _read_chat_stream_source()
    intent_line_match = re.search(
        r'^\s*"intent":\s*([^,\n]+),',
        source,
        re.MULTILINE,
    )
    assert intent_line_match is not None, (
        "Could not find meta frame 'intent' line in chat_stream.py"
    )
    intent_expr = intent_line_match.group(1)
    assert "META_INTENT_L4" in intent_expr, (
        f"L4 (else) branch of meta frame ternary should use META_INTENT_L4 identifier. "
        f"Got: {intent_expr!r}"
    )


def test_chat_stream_uses_routing_layer_constant_for_l1_comparison() -> None:
    """chat_stream preamble checks routing_layer against ROUTING_LAYER_L1 (not "L1")."""
    source = _read_chat_stream_source()
    # Look for the preamble meta frame section: preamble.routing_layer == <expr>
    # After refactor, <expr> should be ROUTING_LAYER_L1, not "L1".
    preamble_section = re.search(
        r'preamble\.routing_layer\s*==\s*([^\n]+)',
        source,
    )
    assert preamble_section is not None, (
        "Could not find 'preamble.routing_layer == ...' comparison in chat_stream.py"
    )
    comparison_expr = preamble_section.group(1).strip()
    # The expression should reference the constant IDENTIFIER (e.g. ROUTING_LAYER_L1),
    # not the bare string value ("L1"). Assert the identifier is present.
    assert "ROUTING_LAYER_L1" in comparison_expr, (
        f"preamble.routing_layer comparison should use ROUTING_LAYER_L1 identifier. "
        f"Got: {comparison_expr!r}"
    )


def test_chat_stream_no_raw_uppercase_kpi_string_outside_documentation() -> None:
    """No raw 'KPI' / 'AGENT_RUN' string literals should remain in chat_stream.py.

    Excludes comments + docstrings. Catches accidental re-introduction of the
    pre-refactor raw strings.
    """
    source = _read_chat_stream_source()
    # Strip comments and docstrings (simple heuristic: lines starting with #,
    # or triple-quoted blocks). Good enough for static guard — not perfect.
    non_comment_lines = []
    in_docstring = False
    quote_char: str | None = None
    for line in source.splitlines():
        stripped = line.strip()
        if in_docstring:
            if quote_char and quote_char in line:
                in_docstring = False
                quote_char = None
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_docstring = True
            quote_char = stripped[:3]
            # Single-line docstring: in_docstring flips on same line below
            if stripped.count(quote_char) >= 2:
                in_docstring = False
                quote_char = None
            continue
        if stripped.startswith("#"):
            continue
        non_comment_lines.append(line)
    code_only = "\n".join(non_comment_lines)
    assert '"KPI"' not in code_only, (
        "Raw \"KPI\" string found in chat_stream.py code. "
        "Use META_INTENT_L1 constant instead."
    )
    assert '"AGENT_RUN"' not in code_only, (
        "Raw \"AGENT_RUN\" string found in chat_stream.py code. "
        "Use META_INTENT_L4 constant instead."
    )
