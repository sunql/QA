"""purpose= 字面量必须走 chat_constants.USAGE_PURPOSE_* SSOT，禁止 bare 字面量。

Static grep-based test: reads service files directly, no DB needed.
Placed at backend/ root to avoid conftest.py autouse DB fixtures in tests/ subdirs.
"""
from __future__ import annotations

import pathlib
import sys

# Bare string literals that must NOT appear as purpose= values in production call sites.
# Excluded: "fallback_*" strings — those are constructed via f"fallback_{purpose}" in
# chat_service._callWithFallback and are intentionally dynamic.
BARE_LITERALS = [
    '"nl2sql"',
    '"chart"',
    '"answer"',
    '"step_plan"',
    '"follow_up_rewrite"',
    '"multistep_global_filter"',
    '"l4_agent_loop"',
    '"clarify"',
    '"supplier_risk"',
    '"agent_run"',
]

TARGETS = [
    "chat_usage.py",
    "chat_multistep.py",
    "chat_stream.py",
    "chat_service.py",
    "chat_l4.py",
    "chat_domain.py",
]


def check() -> list[str]:
    """Return list of error messages; empty list means PASS."""
    base = pathlib.Path(__file__).parent / "app" / "services"
    errors = []
    for t in TARGETS:
        src = (base / t).read_text()
        for lit in BARE_LITERALS:
            if lit not in src:
                continue
            # Match purpose=<literal> but not docstring/comments
            if f"purpose={lit}" in src:
                errors.append(f"{t} still has purpose={lit} literal")
    return errors


if __name__ == "__main__":
    errors = check()
    if errors:
        for e in errors:
            print("FAIL:", e)
        sys.exit(1)
    print("PASS: no bare purpose= literals found")
    sys.exit(0)


def test_no_bare_purpose_literals():
    """purpose= 字面量必须替换为 chat_constants.USAGE_PURPOSE_* 常量。"""
    errors = check()
    assert not errors, "\n".join(errors)
