"""测试库守卫（`app/tests/_pg_support.py`）。

集成夹具每个用例 `TRUNCATE` 全库（CASCADE）。`TEST_DATABASE_URL` 一旦被复制成
生产 DSN，整套测试会清掉本体与业务表 —— Neo4j / Milvus 夹具早有同款 fail-fast，
PG 侧现在也对齐。
"""
from __future__ import annotations

import pytest

from app.tests._pg_support import assertTestDatabase, resolveTestDatabaseUrl

_TEST_URL = "postgresql+asyncpg://qa_user:pw@127.0.0.1:5434/qa_metadata_test"
_SOLO_URL = "postgresql+asyncpg://qa_user:pw@127.0.0.1:5434/qa_metadata_test_solo"
_PROD_URL = "postgresql+asyncpg://qa_user:pw@127.0.0.1:5433/qa_metadata"


@pytest.mark.parametrize("url", [_TEST_URL, _SOLO_URL])
def test_accepts_test_databases(url: str) -> None:
    assertTestDatabase(url)


@pytest.mark.parametrize(
    "url",
    [
        _PROD_URL,
        # 前缀相似但不在白名单：「qa_metadata_test」只能是完整库名或以 `_` 续名
        "postgresql+asyncpg://qa_user:pw@127.0.0.1:5434/qa_metadata_testing",
        "postgresql+asyncpg://qa_user:pw@127.0.0.1:5434/scratch",
        "not-a-dsn",
    ],
)
def test_refuses_non_test_databases(url: str) -> None:
    with pytest.raises(RuntimeError) as excInfo:
        assertTestDatabase(url)
    assert "TRUNCATE" in str(excInfo.value)


def test_resolve_fails_fast_when_env_points_at_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", _PROD_URL)
    with pytest.raises(RuntimeError):
        resolveTestDatabaseUrl()
