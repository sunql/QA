"""回归：`findSuppliersOfMaterial` 必须查 **ItemMaster**（不是已删的 Material）。

2026-11-21 实测：supplier_search 的 materialKey 分支每次都
`TOOL_EXECUTION_FAILED`，根因是 CQL 里写死 `:Material`，而 Phase 4.4 起
物料节点 label 已统一为 `ItemMaster`，`_assertBusinessLabel("Material")`
直接 `ValueError: Invalid business entity label: 'Material'`。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure import neo4j_client


class _FakeRecord(dict):
    pass


class _FakeSession:
    def __init__(self, cqls: list[str]) -> None:
        self._cqls = cqls

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def run(self, cql: str, **params: Any) -> list[_FakeRecord]:
        self._cqls.append(cql)
        return [_FakeRecord(key="D135", code="D135", name="鼎冠模具")]


class _FakeDriver:
    def __init__(self, cqls: list[str]) -> None:
        self._cqls = cqls

    def session(self) -> _FakeSession:
        return _FakeSession(self._cqls)


def test_findSuppliersOfMaterial_queries_ItemMaster_label(monkeypatch) -> None:
    cqls: list[str] = []
    monkeypatch.setattr(neo4j_client, "getDriver", lambda: _FakeDriver(cqls))

    rows = neo4j_client.findSuppliersOfMaterial("AJ0100684", limit=5)

    assert rows and rows[0]["key"] == "D135"
    assert len(cqls) == 1
    assert "ItemMaster" in cqls[0]
    # 旧 label 必须彻底消失（它在 BUSINESS_ENTITY_LABELS 里已不存在）
    assert ":Material" not in cqls[0]


def test_itemMaster_is_a_whitelisted_business_label() -> None:
    """白名单是 CQL 注入防线：ItemMaster 必须在册，Material 必须不在册。"""
    assert "ItemMaster" in neo4j_client.BUSINESS_ENTITY_LABELS
    assert "Material" not in neo4j_client.BUSINESS_ENTITY_LABELS


def test_assertBusinessLabel_rejects_legacy_material_label() -> None:
    with pytest.raises(ValueError):
        neo4j_client._assertBusinessLabel("Material")
