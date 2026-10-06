"""routing_layer 字面量必须走 chat_constants.SSOT，禁止 bare "L1"/"L2"/"L4" 字面量。

Static grep-based test: reads service files directly, no DB needed.
Placed at backend/ root to avoid conftest.py autouse DB fixtures in tests/ subdirs.
"""
from __future__ import annotations

import pathlib
import sys

LITERALS = ['"L1"', '"L2"', '"L4"']
TARGETS = ["chat_service.py", "chat_stream.py", "chat_multistep.py", "chat_l4.py"]


def check() -> list[str]:
    """Return list of error messages; empty list means PASS."""
    base = pathlib.Path(__file__).parent / "app" / "services"
    errors = []
    for t in TARGETS:
        src = (base / t).read_text()
        for lit in LITERALS:
            if lit not in src:
                continue
            if f"routing_layer={lit}" in src:
                errors.append(f"{t} still has routing_layer={lit} literal")
    return errors


if __name__ == "__main__":
    errors = check()
    if errors:
        for e in errors:
            print("FAIL:", e)
        sys.exit(1)
    print("PASS: no bare routing_layer literals found")
    sys.exit(0)


def test_no_bare_routing_layer_literals():
    """routing_layer= 字面量必须替换为 chat_constants.ROUTING_LAYER_* 常量。"""
    errors = check()
    assert not errors, "\n".join(errors)
