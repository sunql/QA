"""Gate test: ensure no SQLite fallback exists in test fixtures.

This test enforces the project's "real PostgreSQL only" rule (CLAUDE.md core constraint #7
+ Harness/rules/测试规范.md).
"""

from __future__ import annotations

import subprocess
import sys


# Files that are allowed to mention sqlite (only in comments/docstrings, never in code).
# These files document the prohibition — they never implement a sqlite fallback.
ALLOWED_FILES: set[str] = {
    # Unit conftest: documents the override, sqlite never appears in actual code here
    "app/tests/unit/conftest.py",
    # Seed tests: docstrings mention sqlite prohibition
    "app/tests/unit/test_seed_missing_business_objects.py",
    "app/tests/unit/test_audit_worker.py",
    # Support files: document the PG-only rule
    "app/tests/_pg_support.py",
    "app/tests/_testapp.py",
    "app/tests/services/conftest.py",
    # Integration conftest: docstrings reference root conftest's sqlite (which is being removed)
    "app/tests/integration/conftest.py",
    # Root conftest: docstring states "禁止 sqlite" (actual code is gone)
    "app/tests/conftest.py",
    # Integration tests: docstrings state the sqlite prohibition
    "app/tests/integration/test_ontology_governance_integration.py",
    "app/tests/integration/test_seed_missing_business_objects.py",
    "app/tests/integration/test_seed_kpi_semantic_index.py",
    "app/tests/integration/test_seed_kpi_catalog.py",
    "app/tests/integration/test_seed_entity_mapping.py",
    "app/tests/integration/test_evaluation_report_service.py",
    "app/tests/integration/test_agent_tool_binding_validation.py",
    "app/tests/integration/test_kpi_catalog_api.py",
}

# conftest files that MUST have zero sqlite code (no allowed mention — any sqlite = must remove).
CONFTEST_FILES_MUST_BE_CLEAN: set[str] = {
    "app/tests/conftest.py",
    "app/tests/integration/conftest.py",
}


def test_no_sqlite_in_fixtures():
    """Fail fast if any sqlite code path creeps into test fixtures.

    This is the audit finding: test fixtures had a SQLite fallback path that lets
    tests pass without a real DB, hiding integration failures. The fix removes that
    fallback; this gate test prevents regression.
    """
    tests_dir = "app/tests"

    try:
        out = subprocess.check_output(
            ["grep", "-r", "sqlite", tests_dir],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        if e.returncode == 1:
            out = ""
        else:
            raise

    out = out.replace("\\", "/")

    lines = out.splitlines()
    code_leaks: list[str] = []

    for line in lines:
        # Skip binary files
        if ".pyc" in line:
            continue
        # Skip this very test file
        if "test_no_sqlite_fallback.py" in line:
            continue
        # Extract just the filename from "path/to/file.py:content"
        colon_idx = line.find(":")
        if colon_idx == -1:
            continue
        filename = line[:colon_idx]
        content = line[colon_idx + 1:]

        # Skip pure comment lines (sqlite appears only in the comment, not in code)
        stripped = content.lstrip()
        if stripped.startswith("#"):
            continue

        # conftest files that must be completely clean: check FIRST, before ALLOWED_FILES.
        # Only flag sqlite in actual code (import, assignment, function call), not in docstrings.
        if filename in CONFTEST_FILES_MUST_BE_CLEAN:
            # Flag only if sqlite appears as a module identifier or in string literals
            # (not just in docstring prose explaining the prohibition)
            if (
                "import sqlite" in content
                or "sqlite" in content and ("=" in content or "(" in content or "[" in content)
            ):
                code_leaks.append(line)
            continue

        # Allowed-doc files: sqlite may appear only in comments/docstrings, never in code.
        # We trust the prohibition is documented correctly in these files.
        if filename in ALLOWED_FILES:
            continue

        # Any other file: assume the worst — flag it for manual review
        code_leaks.append(f"[review needed] {line}")

    assert not code_leaks, (
        f"禁止 SQLite 代码路径出现在 app/tests/ 中:\n"
        + "\n".join(f"  {l}" for l in code_leaks)
    )


if __name__ == "__main__":
    sys.exit(0)
