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


class TestAuditServiceListAllExtensions:
    """8 cases: ILIKE fuzzy actor / actor_departments / since/until / total count / iterAll."""

    def setup_method(self):
        self._svc = AuditService()

    def _mock_session_with_rows(self, rows: list, total: int):
        """Mock session.execute to return a result with scalars().all() and scalar_one().

        `session` is AsyncMock so `await session.execute(...)` is awaitable.
        The result and its scalars() chain are plain MagicMock (synchronous).
        """
        from unittest.mock import AsyncMock, MagicMock
        session = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = rows
        mock_result.scalars.return_value = mock_scalars
        mock_result.scalar_one.return_value = total
        session.execute.return_value = mock_result
        return session

    def test_listAll_actor_ilike_fuzzy(self):
        """actor filter generates ILIKE '%value%', not exact match."""
        import asyncio

        session = self._mock_session_with_rows([], 0)
        asyncio.run(self._svc.listAll(session, actor="张"))
        call_args = session.execute.call_args[0][0]
        compiled = str(call_args.compile(compile_kwargs={"literal_binds": True}))
        # ilike compiles to LIKE + lower() in SQLAlchemy PostgreSQL dialect
        assert "like" in compiled.lower(), f"Expected LIKE, got: {compiled}"
        assert "%" in compiled, f"Expected fuzzy %, got: {compiled}"

    def test_listAll_entity_id_cast_text_ilike(self):
        """entity_id filter casts to TEXT and uses ILIKE '%value%'."""
        import asyncio

        session = self._mock_session_with_rows([], 0)
        asyncio.run(self._svc.listAll(session, entity_id="42"))
        call_args = session.execute.call_args[0][0]
        compiled = str(call_args.compile(compile_kwargs={"literal_binds": True}))
        assert "cast" in compiled.lower(), f"Expected CAST, got: {compiled}"

    def test_listAll_actor_departments_ilike(self):
        """actor_departments filter generates ILIKE '%value%'."""
        import asyncio

        session = self._mock_session_with_rows([], 0)
        asyncio.run(self._svc.listAll(session, actor_departments="采购"))
        call_args = session.execute.call_args[0][0]
        compiled = str(call_args.compile(compile_kwargs={"literal_binds": True}))
        assert "like" in compiled.lower()

    def test_listAll_since_filter(self):
        """since filter generates created_at >= value."""
        import asyncio
        from datetime import datetime, timezone

        session = self._mock_session_with_rows([], 0)
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        asyncio.run(self._svc.listAll(session, since=since))
        call_args = session.execute.call_args[0][0]
        compiled = str(call_args.compile(compile_kwargs={"literal_binds": True}))
        assert ">=" in compiled or "ge" in compiled.lower()

    def test_listAll_until_filter(self):
        """until filter generates created_at < value (exclusive)."""
        import asyncio
        from datetime import datetime, timezone

        session = self._mock_session_with_rows([], 0)
        until = datetime(2026, 9, 1, tzinfo=timezone.utc)
        asyncio.run(self._svc.listAll(session, until=until))
        call_args = session.execute.call_args[0][0]
        compiled = str(call_args.compile(compile_kwargs={"literal_binds": True}))
        assert "<" in compiled

    def test_listAll_returns_tuple_rows_and_total(self):
        """listAll returns (rows, total) tuple. (Merged from T1's TestAuditServiceListAll stub.)"""
        import asyncio
        from unittest.mock import MagicMock

        mock_row = MagicMock()
        session = self._mock_session_with_rows([mock_row], 1)
        rows, total = asyncio.run(self._svc.listAll(session))
        assert isinstance(rows, list)
        assert total == 1

    def test_listAll_total_count_from_subquery(self):
        """total is computed via count subquery, not len(rows)."""
        import asyncio
        from unittest.mock import MagicMock

        session = self._mock_session_with_rows([MagicMock()], 42)
        _, total = asyncio.run(self._svc.listAll(session))
        assert total == 42

    def test_iterAll_yields_all_rows(self):
        """iterAll uses session.stream and yields rows one by one."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        mock_row1, mock_row2 = MagicMock(), MagicMock()
        session = AsyncMock()
        mock_stream_result = MagicMock()
        mock_stream_scalars = MagicMock()
        mock_stream_scalars.__aiter__.return_value = iter([mock_row1, mock_row2])
        mock_stream_result.scalars.return_value = mock_stream_scalars
        # session.stream is awaitable, so use AsyncMock
        async def mock_stream(*args, **kwargs):
            return mock_stream_result
        session.stream = AsyncMock(side_effect=mock_stream)

        results = []
        async def consume():
            async for row in self._svc.iterAll(session):
                results.append(row)

        asyncio.run(consume())
        assert results == [mock_row1, mock_row2]

    def test_iterAll_respects_max_rows(self):
        """iterAll limits to max_rows and logs warning when exceeded."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        session = AsyncMock()
        many_rows = [MagicMock() for _ in range(200)]
        mock_stream_result = MagicMock()
        mock_stream_scalars = MagicMock()
        mock_stream_scalars.__aiter__.return_value = iter(many_rows)
        mock_stream_result.scalars.return_value = mock_stream_scalars
        async def mock_stream(*args, **kwargs):
            return mock_stream_result
        session.stream = AsyncMock(side_effect=mock_stream)

        results = []
        async def consume():
            async for row in self._svc.iterAll(session, max_rows=100):
                results.append(row)

        asyncio.run(consume())
        assert len(results) == 100  # truncated to max_rows
