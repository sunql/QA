"""F9：seed 脚本的目标库解析 —— 只认 ``--database-url``，不猜环境变量。"""

from __future__ import annotations

import pytest

from scripts.seed_kpi_semantic_index import _requireDatabaseUrl


def test_require_database_url_accepts_explicit_dsn() -> None:
    dsn = "postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test_solo"
    assert _requireDatabaseUrl(dsn) == dsn


def test_require_database_url_ignores_database_url_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dev 工作流普遍导出 DATABASE_URL ⇒ 绝不能作为兜底（可能打错库）。"""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5433/qa_metadata"
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test"
    )

    with pytest.raises(SystemExit, match="--database-url"):
        _requireDatabaseUrl(None)
