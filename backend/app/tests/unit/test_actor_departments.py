"""actor_departments 归一守卫（2026-10-30）。

**缺陷背景**：仓内对 `actor_departments` 同时存在两套契约——

- tuple 契约：`AuditService.record` / `OutboxService.enqueue` 声明
  ``tuple[str, ...] | None``；
- 字符串契约：`OntologyService.*` 声明 ``str | None``（本体侧按逗号拆首个部门
  作 `derivedOwner`），API 层按 ``",".join(user.departments)`` 传。

本体侧把**字符串**透传给审计侧后，审计侧执行 ``",".join("采购部")`` ⇒ 落库值
变成 ``采,购,部``（逐字符逗号化）——审计的部门维度从此不可检索。

本文件钉住归一口径：
- tuple/list → 逗号拼接字符串（历史行为不变）；
- 逗号分隔字符串 → **原样**（不再逐字符拆开）；
- 空/None → None。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.actor_departments import departmentsToList, departmentsToString
from app.services.audit_service import AuditService


class TestDepartmentsNormalizer:
    def test_tuple_becomes_comma_string(self) -> None:
        assert departmentsToString(("采购部", "财务部")) == "采购部,财务部"

    def test_string_is_kept_intact(self) -> None:
        """字符串契约：逗号分隔字符串原样保留（**不**逐字符拆）。"""
        assert departmentsToString("采购部,财务部") == "采购部,财务部"

    def test_single_department_string_is_not_split_into_chars(self) -> None:
        """回归靶点：旧实现 `",".join("采购部")` ⇒ "采,购,部"。"""
        assert departmentsToString("采购部") == "采购部"

    def test_none_and_empty_are_none(self) -> None:
        assert departmentsToString(None) is None
        assert departmentsToString(()) is None
        assert departmentsToString("") is None

    def test_list_input_is_accepted(self) -> None:
        assert departmentsToString(["采购部", "财务部"]) == "采购部,财务部"

    def test_string_splits_into_list(self) -> None:
        assert departmentsToList("采购部,财务部") == ["采购部", "财务部"]

    def test_blank_segments_are_dropped(self) -> None:
        assert departmentsToList("采购部,, 财务部 ") == ["采购部", "财务部"]

    def test_tuple_preserves_inner_whitespace(self) -> None:
        """历史行为：tuple 元素不去空白（避免静默改写既有数据语义）。"""
        assert departmentsToList((" 采购部",)) == [" 采购部"]


class TestAuditServiceStoresStringContractIntact:
    """审计落库口径：字符串契约不得被逐字符逗号化。"""

    def _record(self, actorDepartments) -> str | None:
        session = MagicMock()
        svc = AuditService()

        async def _run() -> None:
            await svc.record(
                session,
                entity_type="ontology_class",
                entity_id=1,
                action="CREATE",
                actor="alice",
                actor_departments=actorDepartments,
                after={"id": 1},
            )

        import asyncio

        asyncio.run(_run())
        return session.add.call_args[0][0].actor_departments

    def test_string_contract_not_mangled(self) -> None:
        """本体侧传字符串时，审计落库值必须是 "采购部,财务部"（非逐字符）。"""
        assert self._record("采购部,财务部") == "采购部,财务部"

    def test_tuple_contract_unchanged(self) -> None:
        """tuple 调用方（历史口径）行为不变。"""
        assert self._record(("采购部", "财务部")) == "采购部,财务部"

    def test_single_department_string_not_mangled(self) -> None:
        assert self._record("采购部") == "采购部"

    def test_none_stays_none(self) -> None:
        assert self._record(None) is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
