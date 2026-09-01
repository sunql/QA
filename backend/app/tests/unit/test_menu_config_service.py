"""MenuConfigService 单元测试（stub AsyncSession，无 DB）。

防御分支测试：duplicate-code / section-with-path 走不到正常 ORM 路径，
由 stub 返回手工构造的行覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.menu_config_service import (
    MenuConfigDuplicateError,
    MenuConfigService,
    MenuConfigStructureError,
)


@dataclass
class _Row:
    code: str
    parent_id: int | None = None
    label_key: str = "k"
    icon_code: str | None = None
    sort_order: int = 0
    permission_code: str | None = None
    roles: str | None = None
    path: str | None = None
    id: int = 0


class _StubSession:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    async def execute(self, _stmt):
        return _StubResult(self._rows)


class _StubResult:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


async def test_list_sections_duplicate_code_raises() -> None:
    rows = [
        _Row(id=1, code="dup", sort_order=100),
        _Row(id=2, code="dup", sort_order=200),
    ]
    with pytest.raises(MenuConfigDuplicateError, match="menu_config duplicate code: dup"):
        await MenuConfigService(_StubSession(rows)).list_sections()


async def test_list_sections_section_with_path_raises() -> None:
    rows = [_Row(id=1, code="section.bad", sort_order=100, path="/bad")]
    with pytest.raises(MenuConfigStructureError):
        await MenuConfigService(_StubSession(rows)).list_sections()
