"""Override autouse fixtures so pure-schema unit tests don't need a live DB."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

# Override warmBusinessObjectRegistry — no-op since this test file uses no DB.
@pytest_asyncio.fixture(autouse=True)
async def warmBusinessObjectRegistry() -> AsyncIterator[None]:
    yield


# Override seedEngine — return mock factory/engine so the test file's imports don't fail.
@pytest.fixture()
async def seedEngine() -> tuple[MagicMock, MagicMock]:
    factory = MagicMock()
    engine = MagicMock()
    return factory, engine


# Override dbSession — not used by these tests but satisfies any implicit deps.
@pytest_asyncio.fixture()
async def dbSession() -> AsyncIterator[AsyncMock]:
    yield AsyncMock(spec=AsyncSession)
