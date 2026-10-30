"""DataDomainRegistry 单测（feat-data-domain-layer 方案 1）.

钉死：
- warmUp 加载 (allCodes, enabledCodes)；isValid 仅 enabled 通过
- 未 warmed → RuntimeError（fail-fast，同 businessObjectRegistry）
- invalidate 后 isWarmedUp()=False；reloadOne 处理 enable/disable/delete 三态
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.data_domain_registry import DataDomainRegistry


def _warmUpSession(rows: list) -> AsyncMock:
    """warmUp 用：session.execute(...).all() → rows。"""
    result = MagicMock()
    result.all.return_value = rows
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


def _reloadSession(row) -> AsyncMock:
    """reloadOne 用：session.execute(...).first() → row（None=行不存在）。"""
    result = MagicMock()
    result.first.return_value = row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


def test_unwarmed_raises() -> None:
    registry = DataDomainRegistry()
    assert registry.isWarmedUp() is False
    with pytest.raises(RuntimeError):
        registry.isValid("PROCUREMENT")
    with pytest.raises(RuntimeError):
        registry.getEnabled()


def test_warm_up_splits_enabled_and_all() -> None:
    registry = DataDomainRegistry()
    session = _warmUpSession([
        ("PROCUREMENT", True), ("QUALITY", True), ("DISABLED_DOM", False),
    ])
    asyncio.run(registry.warmUp(session))
    assert registry.isWarmedUp() is True
    assert registry.isValid("PROCUREMENT") is True
    assert registry.isValid("DISABLED_DOM") is False  # 禁用域不可用于新写入
    assert registry.isValid("UNKNOWN") is False
    assert registry.getEnabled() == frozenset({"PROCUREMENT", "QUALITY"})
    assert registry.getAll() == frozenset({"PROCUREMENT", "QUALITY", "DISABLED_DOM"})


def test_invalidate_clears_state() -> None:
    registry = DataDomainRegistry()
    session = _warmUpSession([("PROCUREMENT", True)])
    asyncio.run(registry.warmUp(session))
    registry.invalidate()
    assert registry.isWarmedUp() is False
    with pytest.raises(RuntimeError):
        registry.getEnabled()
    # 未 warmed 时 invalidate 是 no-op
    registry.invalidate()


def test_reload_one_enable_disable_delete() -> None:
    registry = DataDomainRegistry()
    session = _warmUpSession([("PROCUREMENT", True), ("QUALITY", False)])
    asyncio.run(registry.warmUp(session))

    # disable：QUALITY 置 enabled=True → 进 enabled
    session2 = _reloadSession(("QUALITY", True))
    asyncio.run(registry.reloadOne(session2, "QUALITY"))
    assert registry.isValid("QUALITY") is True

    # delete：行消失 → 两集合均移除
    session3 = _reloadSession(None)
    asyncio.run(registry.reloadOne(session3, "PROCUREMENT"))
    assert registry.isValid("PROCUREMENT") is False
    assert "PROCUREMENT" not in registry.getAll()
