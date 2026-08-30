"""AuditService 单元测试（Phase 4.5 governance hardening）。

参数校验（ValueError 早暴露）+ session.add 行为（不 commit）。
DB 层写校验由 CheckConstraint 兜底，由集成测试覆盖。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.audit_service import AuditService


def _mockSession() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    return s


class TestAuditServiceRecord:
    def setup_method(self) -> None:
        self._svc = AuditService()
        self._session = _mockSession()

    def test_create_requires_after(self) -> None:
        import asyncio

        with pytest.raises(ValueError, match="CREATE 必须提供 after"):
            asyncio.run(
                self._svc.record(
                    self._session,
                    entity_type="kpi_catalog",
                    entity_id=1,
                    action="CREATE",
                    actor="alice",
                    after=None,
                )
            )

    def test_delete_requires_before(self) -> None:
        import asyncio

        with pytest.raises(ValueError, match="DELETE 必须提供 before"):
            asyncio.run(
                self._svc.record(
                    self._session,
                    entity_type="kpi_catalog",
                    entity_id=1,
                    action="DELETE",
                    actor="alice",
                    before=None,
                )
            )

    def test_update_requires_both(self) -> None:
        import asyncio

        with pytest.raises(ValueError, match="UPDATE 必须提供 before 与 after"):
            asyncio.run(
                self._svc.record(
                    self._session,
                    entity_type="kpi_catalog",
                    entity_id=1,
                    action="UPDATE",
                    actor="alice",
                    before={"x": 1},
                    after=None,
                )
            )
        with pytest.raises(ValueError, match="UPDATE 必须提供 before 与 after"):
            asyncio.run(
                self._svc.record(
                    self._session,
                    entity_type="kpi_catalog",
                    entity_id=1,
                    action="UPDATE",
                    actor="alice",
                    before=None,
                    after={"x": 2},
                )
            )

    def test_invalid_action_rejected(self) -> None:
        import asyncio

        with pytest.raises(ValueError, match="audit action 非法"):
            asyncio.run(
                self._svc.record(
                    self._session,
                    entity_type="kpi_catalog",
                    entity_id=1,
                    action="INVALID",
                    actor="alice",
                    before={},
                    after={},
                )
            )

    def test_record_does_not_commit(self) -> None:
        """record 只 session.add，不 commit — 调用方需 commit。"""
        import asyncio

        asyncio.run(
            self._svc.record(
                self._session,
                entity_type="kpi_catalog",
                entity_id=1,
                action="CREATE",
                actor="alice",
                after={"id": 1, "kpi_code": "X"},
            )
        )
        self._session.add.assert_called_once()
        self._session.commit.assert_not_called()
        self._session.flush.assert_not_called()

    def test_departments_joined_with_comma(self) -> None:
        import asyncio

        asyncio.run(
            self._svc.record(
                self._session,
                entity_type="kpi_catalog",
                entity_id=1,
                action="CREATE",
                actor="alice",
                actor_departments=("采购部", "财务部"),
                after={"id": 1},
            )
        )
        callArg = self._session.add.call_args[0][0]
        assert callArg.actor_departments == "采购部,财务部"

    def test_none_departments_stored_as_none(self) -> None:
        """departments=None 时 actor_departments 字段存 None（不写空字符串）。"""
        import asyncio

        asyncio.run(
            self._svc.record(
                self._session,
                entity_type="kpi_catalog",
                entity_id=1,
                action="CREATE",
                actor="alice",
                actor_departments=None,
                after={"id": 1},
            )
        )
        callArg = self._session.add.call_args[0][0]
        assert callArg.actor_departments is None

    def test_empty_tuple_departments_treated_as_none(self) -> None:
        """空 tuple → None（与 None 等价；用户无部门信息）。

        当前实现：None 与 () 都映射为 None（避免存储空字符串带来的语义模糊）。
        """
        import asyncio

        asyncio.run(
            self._svc.record(
                self._session,
                entity_type="kpi_catalog",
                entity_id=1,
                action="CREATE",
                actor="alice",
                actor_departments=(),
                after={"id": 1},
            )
        )
        callArg = self._session.add.call_args[0][0]
        assert callArg.actor_departments is None

    def test_create_with_after_sets_fields(self) -> None:
        """CREATE 写入后：action / actor / entity_type / entity_id / after_json 全到位。"""
        import asyncio

        asyncio.run(
            self._svc.record(
                self._session,
                entity_type="kpi_catalog",
                entity_id=42,
                action="CREATE",
                actor="alice",
                actor_departments=("采购部",),
                after={"id": 42, "kpi_code": "X"},
            )
        )
        callArg = self._session.add.call_args[0][0]
        assert callArg.entity_type == "kpi_catalog"
        assert callArg.entity_id == 42
        assert callArg.action == "CREATE"
        assert callArg.actor == "alice"
        assert callArg.after_json == {"id": 42, "kpi_code": "X"}
        assert callArg.before_json is None

    def test_update_passes_through_before_and_after(self) -> None:
        import asyncio

        asyncio.run(
            self._svc.record(
                self._session,
                entity_type="kpi_catalog",
                entity_id=1,
                action="UPDATE",
                actor="alice",
                before={"formula": "OLD"},
                after={"formula": "NEW"},
            )
        )
        callArg = self._session.add.call_args[0][0]
        assert callArg.before_json == {"formula": "OLD"}
        assert callArg.after_json == {"formula": "NEW"}

    def test_delete_has_no_after(self) -> None:
        import asyncio

        asyncio.run(
            self._svc.record(
                self._session,
                entity_type="kpi_catalog",
                entity_id=1,
                action="DELETE",
                actor="alice",
                before={"id": 1},
            )
        )
        callArg = self._session.add.call_args[0][0]
        assert callArg.before_json == {"id": 1}
        assert callArg.after_json is None
