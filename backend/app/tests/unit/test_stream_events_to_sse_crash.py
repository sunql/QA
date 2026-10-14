"""F3 RED test: set/frozenset/bytes in event data must not crash toSse.

The docstring of _jsonSafe promises "default=str" as fallback, but json.dumps
in toSse() has no default= parameter.  These types hit the bare return value
and cause TypeError mid-stream.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.services.stream_events import StreamEvent


class TestToSseSetFrozensetBytes:
    """Gate 1-3: set/frozenset/bytes must not raise, must produce parseable JSON."""

    def test_set_does_not_raise_and_produces_valid_json(self):
        """set via toSse() yields a parseable SSE frame (not just no-exception)."""
        event = StreamEvent("data", {"v": {"a", "b"}})
        text = event.toSse()  # must not raise TypeError
        # forward assertion: data line must be legal JSON
        data_line = next(line for line in text.split("\n") if line.startswith("data:"))
        payload = data_line.removeprefix("data: ")
        parsed = json.loads(payload)  # must not raise
        assert "v" in parsed

    def test_frozenset_does_not_raise_and_produces_valid_json(self):
        """frozenset is stable for assertion (no PYTHONHASHSEED non-determinism)."""
        event = StreamEvent("data", {"v": frozenset({1, 2, 3})})
        text = event.toSse()  # must not raise TypeError
        data_line = next(line for line in text.split("\n") if line.startswith("data:"))
        payload = data_line.removeprefix("data: ")
        parsed = json.loads(payload)  # must not raise
        assert parsed["v"] == "frozenset({1, 2, 3})"

    def test_bytes_does_not_raise_and_produces_valid_json(self):
        """bytes (e.g. binary column) must not raise TypeError."""
        event = StreamEvent("data", {"v": b"\x00\xff"})
        text = event.toSse()  # must not raise TypeError
        data_line = next(line for line in text.split("\n") if line.startswith("data:"))
        payload = data_line.removeprefix("data: ")
        parsed = json.loads(payload)  # must not raise
        assert "v" in parsed

    def test_arbitrary_object_does_not_raise(self):
        """Custom object falls through to default=str and must not raise."""

        class MyObj:
            def __init__(self, x):
                self.x = x

        event = StreamEvent("data", {"v": MyObj(42)})
        text = event.toSse()  # must not raise
        data_line = next(line for line in text.split("\n") if line.startswith("data:"))
        payload = data_line.removeprefix("data: ")
        parsed = json.loads(payload)
        assert "v" in parsed


class TestToSseNoSemanticRegression:
    """Gate 2: P0-4 semantics must not regress (Decimal->str, non-finite sentinel)."""

    def test_decimal_still_serializes_as_string_not_float(self):
        """Decimal('1.10') must become '1.10' (str), not 1.1 (float)."""
        event = StreamEvent("data", {"amount": Decimal("1.10")})
        text = event.toSse()
        data_line = next(line for line in text.split("\n") if line.startswith("data:"))
        payload = data_line.removeprefix("data: ")
        parsed = json.loads(payload)
        assert parsed["amount"] == "1.10", "Decimal must NOT become float"

    def test_positive_infinity_still_becomes_sentinel(self):
        """+Inf must become __non_finite__ sentinel, not literal Infinity."""
        event = StreamEvent("data", {"v": float("inf")})
        text = event.toSse()
        data_line = next(line for line in text.split("\n") if line.startswith("data:"))
        payload = data_line.removeprefix("data: ")
        parsed = json.loads(payload)
        assert parsed["v"] == {"__non_finite__": "infinity"}

    def test_negative_infinity_still_becomes_sentinel(self):
        event = StreamEvent("data", {"v": float("-inf")})
        text = event.toSse()
        data_line = next(line for line in text.split("\n") if line.startswith("data:"))
        payload = data_line.removeprefix("data: ")
        parsed = json.loads(payload)
        assert parsed["v"] == {"__non_finite__": "-infinity"}

    def test_nan_still_becomes_sentinel(self):
        event = StreamEvent("data", {"v": float("nan")})
        text = event.toSse()
        data_line = next(line for line in text.split("\n") if line.startswith("data:"))
        payload = data_line.removeprefix("data: ")
        parsed = json.loads(payload)
        assert parsed["v"] == {"__non_finite__": "nan"}
