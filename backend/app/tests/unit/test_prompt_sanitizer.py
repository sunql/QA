"""Unit tests for the L1 prompt sanitizer public API.

Focus: the ``wrapSanitized`` helper (wrap ALREADY sanitized text, used by
callers that must bound the *rendered* length) and the unchanged
``wrapUntrusted`` contract.
"""

from __future__ import annotations

import pytest

from app.security.prompt_sanitizer import (
    UntrustedLabel,
    sanitizeForLlmPrompt,
    wrapSanitized,
    wrapUntrusted,
)


def test_wrap_untrusted_escapes_exactly_once():
    wrapped = wrapUntrusted(UntrustedLabel.USER_QUESTION, 'a"b')
    assert wrapped == "<user_question>a&quot;b</user_question>"
    assert wrapped.count("&quot;") == 1


def test_wrap_sanitized_does_not_escape_again():
    safe = sanitizeForLlmPrompt('a"b')
    wrapped = wrapSanitized(UntrustedLabel.STEP_RESULT, safe)
    assert wrapped == "<step_result>a&quot;b</step_result>"
    assert wrapped.count("&quot;") == 1


def test_wrap_sanitized_keeps_label_whitelist():
    with pytest.raises(ValueError):
        wrapSanitized("not_in_whitelist", "text")
    with pytest.raises(ValueError):
        wrapSanitized("BadLabel", "text")


def test_wrap_sanitized_and_wrap_untrusted_agree_on_valid_text():
    text = "plain text"
    assert wrapSanitized(
        UntrustedLabel.AGENT_TOOL_RESULT, sanitizeForLlmPrompt(text)
    ) == wrapUntrusted(UntrustedLabel.AGENT_TOOL_RESULT, text)
