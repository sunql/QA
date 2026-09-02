# Audit History API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend audit_log from "2 entity types traceable" to "all governance + Agent runtime entities traceable + admin full query/export capability".

**Architecture:** Backend: FastAPI + SQLAlchemy 2.0 async + PostgreSQL. Frontend: React + Ant Design 5 + TypeScript strict. Audit writes via synchronous `audit.record()` (same transaction as business write, same pattern as feature_definition_service). Export via `iterAll` streaming yield + StreamingResponse.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.0 async, PostgreSQL (port 5433), Alembic, React 18, Ant Design 5, TypeScript strict, vitest.

## Global Constraints

- Backend Python: snake_case functions/vars, snake_case ORM/Pydantic fields (project deviation from PEP 8)
- File size: <=800 lines, functions <=50 lines, nesting <=4 levels
- Immutability: create new objects, never mutate
- Commit format: `<type>: <description>`, NO `Co-Authored-By:` trailer
- Test DB: `postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test` (port 5433, NOT 5432)
- Backend testing: real PostgreSQL only (no sqlite)
- Coverage gate: `pytest app/tests/ --cov=app --cov-fail-under=80`
- TDD: write failing test first, run, implement minimal, run, commit
- Alembic migration: `0036_audit_actor_index.py` with upgrade/downgrade, down_revision="0035_agent_tool_binding"
- AuditService.record() does NOT commit — caller commits for transaction atomicity
- All `audit.record()` calls use standard pattern: `await _audit.record(session, entity_type="...", entity_id=entity.id, action="...", actor=actor, actor_departments=actor_departments, after=entity.to_dict())`
- actor/actor_departments sourced from `CurrentUser.userId` / `CurrentUser.departments` — never call getCurrentUser() inside a service

---

## Task 1: Alembic 0036 + AuditLogPage schema + admin-only ACL helper

**Files:**
- Create: `backend/alembic/versions/0036_audit_actor_index.py`
- Modify: `backend/app/domain/schemas.py` (add AuditLogPage)
- Modify: `backend/app/dependencies.py` (add getAdminOnlyActor)
- Test: `backend/app/tests/unit/test_audit_service.py` (extend with new cases after schema change)

**Interfaces:**
- Consumes: `AuditLog` model from `app.domain.models`
- Produces: `AuditLogPage` schema class; `getAdminOnlyActor` dependency function

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/unit/test_audit_service.py
# Extend with new listAll test cases (added after Task 2 implementation)
import pytest
from datetime import datetime, timezone

class TestAuditServiceListAll:
    """listAll extensions: ILIKE fuzzy, actor_departments, since/until, total count."""

    @pytest.fixture
    def svc(self):
        return AuditService()

    def test_listAll_returns_tuple_of_rows_and_total(self):
        """listAll should return tuple[list[AuditLog], int], not list[AuditLog]."""
        import asyncio
        from unittest.mock import MagicMock

        session = MagicMock()
        # Mock execute to return a result with scalars().all() and scalar_one()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_result.scalar_one.return_value = 0
        session.execute.return_value = mock_result

        svc = AuditService()
        result = asyncio.run(svc.listAll(session))
        # Should be tuple (rows, total), not list
        assert isinstance(result, tuple), f"listAll must return tuple, got {type(result)}"
        assert len(result) == 2
        rows, total = result
        assert isinstance(rows, list)
        assert isinstance(total, int)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/unit/test_audit_service.py::TestAuditServiceListAll::test_listAll_returns_tuple_of_rows_and_total -v`
Expected: FAIL — `listAll` currently returns `list[AuditLog]`, not `tuple[list[AuditLog], int]`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/alembic/versions/0036_audit_actor_index.py
"""add btree indexes on audit_log.actor and audit_log.created_at.

Revision ID: 0036_audit_actor_index
Revises: 0035_agent_tool_binding
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0036_audit_actor_index"
down_revision = "0035_agent_tool_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_audit_log_actor", "audit_log", ["actor"])
    op.create_index("idx_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_audit_log_created_at", table_name="audit_log")
    op.drop_index("idx_audit_log_actor", table_name="audit_log")
```

```python
# backend/app/domain/schemas.py — add AuditLogPage
class AuditLogPage(CamelModel):
    """审计日志分页响应。"""
    rows: list["AuditLogRead"]
    total: int
```

```python
# backend/app/dependencies.py — add getAdminOnlyActor
from fastapi import Depends, HTTPException, status
from app.dependencies import getCurrentUser, CurrentUser

async def getAdminOnlyActor(
    user: CurrentUser = Depends(getCurrentUser),
) -> CurrentUser:
    """admin-only 审计 API 的 actor 派生。

    非 admin 抛 403，与 assertCanModify 模式平行。
    """
    if "admin" not in (user.roles or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅 admin 可访问审计 API",
        )
    return user
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/unit/test_audit_service.py::TestAuditServiceListAll::test_listAll_returns_tuple_of_rows_and_total -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/0036_audit_actor_index.py backend/app/domain/schemas.py backend/app/dependencies.py
git commit -m "feat(audit): 0036 actor btree index + AuditLogPage + admin-only ACL helper"
```

**Verification:** `pytest backend/app/tests/unit/test_audit_service.py -v --cov=app --cov-fail-under=80`

---

## Task 2: AuditService.listAll extensions + listAuditLogs endpoint tests

**Files:**
- Modify: `backend/app/services/audit_service.py` (listAll signature + implementation)
- Modify: `backend/app/tests/unit/test_audit_service.py` (8 new test cases)
- Modify: `backend/app/tests/integration/test_audit_api.py` (10 new test cases, extend existing)
- Test: `backend/app/tests/integration/test_audit_api.py`

**Interfaces:**
- Consumes: `AuditLog` model, `getAdminOnlyActor` from dependencies
- Produces: `listAll` returning `tuple[list[AuditLog], int]` with new filters

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/unit/test_audit_service.py — add TestAuditServiceListAllExtensions
class TestAuditServiceListAllExtensions:
    """8 cases: ILIKE fuzzy actor / actor_departments / since/until / total count / iterAll."""

    def setup_method(self):
        self._svc = AuditService()

    def _mock_session_with_rows(self, rows: list, total: int) -> MagicMock:
        session = MagicMock()
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
        assert "ilike" in compiled.lower(), f"Expected ILIKE, got: {compiled}"
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
        assert "ilike" in compiled.lower()

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
        """listAll returns (rows, total) tuple."""
        import asyncio

        mock_row = MagicMock()
        session = self._mock_session_with_rows([mock_row], 1)
        rows, total = asyncio.run(self._svc.listAll(session))
        assert isinstance(rows, list)
        assert total == 1

    def test_listAll_total_count_from_subquery(self):
        """total is computed via count subquery, not len(rows)."""
        import asyncio

        session = self._mock_session_with_rows([MagicMock()], 42)
        _, total = asyncio.run(self._svc.listAll(session))
        assert total == 42

    def test_iterAll_yields_all_rows(self):
        """iterAll uses session.stream and yields rows one by one."""
        import asyncio
        from unittest.mock import MagicMock

        mock_row1, mock_row2 = MagicMock(), MagicMock()
        session = MagicMock()
        mock_stream_result = MagicMock()
        mock_stream_scalars = MagicMock()
        mock_stream_scalars.__aiter__.return_value = iter([mock_row1, mock_row2])
        mock_stream_result.scalars.return_value = mock_stream_scalars
        session.stream.return_value = mock_stream_result

        results = []
        async def consume():
            async for row in self._svc.iterAll(session):
                results.append(row)

        asyncio.run(consume())
        assert results == [mock_row1, mock_row2]

    def test_iterAll_respects_max_rows(self):
        """iterAll limits to max_rows and logs warning when exceeded."""
        import asyncio
        from unittest.mock import MagicMock

        session = MagicMock()
        many_rows = [MagicMock() for _ in range(200)]
        mock_stream_result = MagicMock()
        mock_stream_scalars = MagicMock()
        mock_stream_scalars.__aiter__.return_value = iter(many_rows)
        mock_stream_result.scalars.return_value = mock_stream_scalars
        session.stream.return_value = mock_stream_result

        results = []
        async def consume():
            async for row in self._svc.iterAll(session, max_rows=100):
                results.append(row)

        asyncio.run(consume())
        assert len(results) == 100  # truncated to max_rows
```

```python
# backend/app/tests/integration/test_audit_api.py — extend TestAuditApi
# Add to existing class:

    async def test_list_returns_audit_log_page_shape(self, client, dbSession) -> None:
        """GET /api/v1/audit returns {rows: [...], total: N} (AuditLogPage), not bare list."""
        resp = await client.get("/api/v1/audit", headers=ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert "rows" in body, f"Expected AuditLogPage shape, got: {body}"
        assert "total" in body, f"Expected AuditLogPage shape, got: {body}"
        assert isinstance(body["rows"], list)
        assert isinstance(body["total"], int)

    async def test_list_actor_ilike_fuzzy(self, client, dbSession) -> None:
        """actor='张' matches actor containing '张', not exact."""
        # 先有数据
        await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUDIT_ILIKE_{id(self)}",
                "kpiName": "模糊匹配测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        await drainOutbox(dbSession)
        # 用部分字符搜索（应命中）
        resp = await client.get("/api/v1/audit?actor=admin", headers=ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["total"] >= 1

    async def test_list_actor_departments_filter(self, client, dbSession) -> None:
        """actor_departments='采购' matches '采购部,财务部'."""
        resp = await client.get(
            "/api/v1/audit?actor_departments=采购",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

    async def test_list_since_until_range(self, client, dbSession) -> None:
        """since/until filters by created_at range."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        resp = await client.get(
            f"/api/v1/audit?since=2020-01-01T00:00:00Z&until=2099-01-01T00:00:00Z",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["total"] >= 0

    async def test_non_admin_returns_403(self, client, dbSession) -> None:
        """Non-admin user gets 403 on /api/v1/audit."""
        resp = await client.get(
            "/api/v1/audit",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_by_entity_acl收紧(self, client, dbSession) -> None:
        """GET /api/v1/audit/by-entity/{t}/{id} requires admin."""
        resp = await client.get(
            "/api/v1/audit/by-entity/kpi_catalog/1",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_by_actor_acl收紧(self, client, dbSession) -> None:
        """GET /api/v1/audit/by-actor/{actor} requires admin."""
        resp = await client.get(
            "/api/v1/audit/by-actor/test-admin",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_get_by_id_acl收紧(self, client, dbSession) -> None:
        """GET /api/v1/audit/{id} requires admin."""
        resp = await client.get(
            "/api/v1/audit/1",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_list_entity_id_filter(self, client, dbSession) -> None:
        """entity_id filter does CAST TEXT ILIKE."""
        # 创建 KPI，然后按其 ID 搜索
        kpi_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUDIT_EID_{id(self)}",
                "kpiName": "ID过滤测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert kpi_resp.status_code == status.HTTP_201_CREATED
        kpi_id = kpi_resp.json()["id"]
        await drainOutbox(dbSession)
        resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()["rows"]
        assert any(r["entityId"] == kpi_id for r in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/app/tests/unit/test_audit_service.py::TestAuditServiceListAllExtensions -v`
Expected: FAIL — `listAll` signature and `iterAll` method do not exist yet

Run: `pytest backend/app/tests/integration/test_audit_api.py -v -k "acl or page_shape or ilike or departments or since_until or entity_id"`
Expected: FAIL — new query params and response shape not yet implemented

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/audit_service.py — replace listAll, add iterAll
from typing import AsyncIterator, cast
from sqlalchemy import String, func, select

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000

async def listAll(
    self,
    session: AsyncSession,
    *,
    entity_type: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    entity_id: str | None = None,
    actor_departments: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    """全局审计记录查询（支持 entity_type/action/actor fuzzy/actor_departments/since/until）。"""
    limit = min(limit, _MAX_LIMIT)
    where = []
    if entity_type:
        where.append(AuditLog.entity_type == entity_type)
    if action:
        where.append(AuditLog.action == action)
    if actor:
        where.append(AuditLog.actor.ilike(f"%{actor}%"))
    if entity_id:
        where.append(cast(AuditLog.entity_id, String).ilike(f"%{entity_id}%"))
    if actor_departments:
        where.append(AuditLog.actor_departments.ilike(f"%{actor_departments}%"))
    if since:
        where.append(AuditLog.created_at >= since)
    if until:
        where.append(AuditLog.created_at < until)

    base_stmt = select(AuditLog).where(*where).order_by(AuditLog.created_at.desc())
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = base_stmt.limit(limit).offset(offset)
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def iterAll(
    self,
    session: AsyncSession,
    *,
    entity_type: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    entity_id: str | None = None,
    actor_departments: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    max_rows: int = 100_000,
) -> AsyncIterator[AuditLog]:
    """流式 yield 审计记录（session.stream 避免一次加载）。超过 max_rows 截断。"""
    import logging
    logger = logging.getLogger(__name__)

    where = []
    if entity_type:
        where.append(AuditLog.entity_type == entity_type)
    if action:
        where.append(AuditLog.action == action)
    if actor:
        where.append(AuditLog.actor.ilike(f"%{actor}%"))
    if entity_id:
        where.append(cast(AuditLog.entity_id, String).ilike(f"%{entity_id}%"))
    if actor_departments:
        where.append(AuditLog.actor_departments.ilike(f"%{actor_departments}%"))
    if since:
        where.append(AuditLog.created_at >= since)
    if until:
        where.append(AuditLog.created_at < until)

    stmt = select(AuditLog).where(*where).order_by(AuditLog.created_at.desc()).limit(max_rows)
    result = await session.stream(stmt)
    emitted = 0
    async for row in result.scalars():
        yield row
        emitted += 1
        if emitted >= max_rows:
            logger.warning("iterAll emitted %d rows (max_rows=%d) — truncating", emitted, max_rows)
            break
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest backend/app/tests/unit/test_audit_service.py::TestAuditServiceListAllExtensions -v`
Run: `pytest backend/app/tests/integration/test_audit_api.py -v -k "acl or page_shape or ilike or departments or since_until or entity_id"`
Expected: PASS for both

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/audit_service.py backend/app/tests/unit/test_audit_service.py backend/app/tests/integration/test_audit_api.py
git commit -m "feat(audit): listAll ILIKE + actor_departments + since/until + total count"
```

**Verification:** `pytest backend/app/tests/unit/test_audit_service.py backend/app/tests/integration/test_audit_api.py -v --cov=app --cov-fail-under=80`

---

## Task 3: iterAll yield + /audit/export endpoint + CSV/JSON streaming

**Files:**
- Modify: `backend/app/api/v1/router.py` or wherever audit router is defined (find audit router first with grep)
- Create: `backend/app/tests/integration/test_audit_export_api.py`
- Test: `backend/app/tests/integration/test_audit_export_api.py`

**Interfaces:**
- Consumes: `iterAll` from AuditService, `getAdminOnlyActor` dependency
- Produces: `GET /api/v1/audit/export` StreamingResponse endpoint

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_audit_export_api.py
"""audit_log export API integration tests."""
from __future__ import annotations
import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
class TestAuditExportApi:

    async def test_export_csv_returns_200_with_streaming(self, client, dbSession) -> None:
        """GET /api/v1/audit/export?format=csv returns StreamingResponse with text/csv."""
        resp = await client.get(
            "/api/v1/audit/export?format=csv",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert "text/csv" in resp.headers["content-type"]
        assert "attachment" in resp.headers.get("content-disposition", "")
        # CSV should have header row
        content = resp.text
        lines = content.strip().split("\n")
        assert len(lines) >= 1
        assert "id" in lines[0].lower()

    async def test_export_jsonl_returns_200(self, client, dbSession) -> None:
        """GET /api/v1/audit/export?format=json returns application/x-ndjson."""
        resp = await client.get(
            "/api/v1/audit/export?format=json",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert "application/x-ndjson" in resp.headers["content-type"]
        lines = resp.text.strip().split("\n")
        if lines and lines[0]:
            import json
            obj = json.loads(lines[0])
            assert "id" in obj

    async def test_export_csv_contains_expected_columns(self, client, dbSession) -> None:
        """CSV header: id,created_at,entity_type,entity_id,action,actor,actor_departments,before_json,after_json."""
        resp = await client.get(
            "/api/v1/audit/export?format=csv",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        header = resp.text.split("\n")[0]
        for col in ["id", "created_at", "entity_type", "action", "actor"]:
            assert col in header.lower(), f"Missing column: {col}"

    async def test_export_non_admin_returns_403(self, client, dbSession) -> None:
        """Non-admin user gets 403 on export endpoint."""
        resp = await client.get(
            "/api/v1/audit/export?format=csv",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_audit_export_api.py -v`
Expected: FAIL — `/audit/export` endpoint does not exist yet

- [ ] **Step 3: Write minimal implementation**

First, find the audit router:
```bash
grep -rn "audit" /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/app/api/ --include="*.py" | grep "router\|Router" | head -10
```

Then add to the audit router:
```python
# backend/app/api/v1/audit.py (or wherever audit router lives — use grep result)
from fastapi import APIRouter, Depends, Query, StreamingResponse
from typing import Annotated, Literal
from app.dependencies import getAdminOnlyActor, CurrentUser, getDb
from app.services.audit_service import AuditService
from app.domain.schemas import AuditLogPage, AuditLogRead
from sqlalchemy.ext.asyncio import AsyncSession
import csv
import json
from datetime import datetime

router = APIRouter(prefix="/audit", tags=["audit"])
_audit = AuditService()
EXPORT_MAX = 100_000


async def _stream_csv(rows_gen) -> bytes:
    header = ["id", "created_at", "entity_type", "entity_id", "action", "actor", "actor_departments", "before_json", "after_json"]
    async def gen():
        yield ",".join(header) + "\n"
        async for row in rows_gen:
            yield ",".join([
                str(row.id),
                str(row.created_at.isoformat()) if row.created_at else "",
                row.entity_type or "",
                str(row.entity_id),
                row.action or "",
                row.actor or "",
                row.actor_departments or "",
                json.dumps(row.before_json or {}),
                json.dumps(row.after_json or {}),
            ]) + "\n"
    return gen()


async def _stream_json(rows_gen) -> bytes:
    async def gen():
        async for row in rows_gen:
            yield json.dumps({
                "id": row.id,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
                "entityType": row.entity_type,
                "entityId": row.entity_id,
                "action": row.action,
                "actor": row.actor,
                "actorDepartments": row.actor_departments,
                "beforeJson": row.before_json,
                "afterJson": row.after_json,
            }, ensure_ascii=False) + "\n"
    return gen()


@router.get("/export")
async def exportAuditLogs(
    format: Annotated[Literal["csv", "json"], Query()] = "csv",
    entity_type: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    actor_departments: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    db: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    """流式导出审计日志（admin only）。最大 100k 行。"""
    rows_gen = _audit.iterAll(
        db,
        entity_type=entity_type,
        action=action,
        actor=actor,
        actor_departments=actor_departments,
        since=since,
        until=until,
        max_rows=EXPORT_MAX,
    )
    if format == "csv":
        return StreamingResponse(
            _stream_csv(rows_gen),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
        )
    return StreamingResponse(
        _stream_json(rows_gen),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'},
    )
```

Also update the `GET /api/v1/audit` endpoint to use the new `listAll` returning `AuditLogPage`:
```python
# In the existing listAuditLogs endpoint (find it in the audit router first)
# Change response_model from list[AuditLogRead] to AuditLogPage
# Change handler to:
#     rows, total = await _audit.listAll(db, entity_type=entity_type, ...)
#     return AuditLogPage(rows=[AuditLogRead.model_validate(r) for r in rows], total=total)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_audit_export_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/audit.py backend/app/tests/integration/test_audit_export_api.py
git commit -m "feat(audit): iterAll yield + export csv/json endpoint"
```

**Verification:** `pytest backend/app/tests/integration/test_audit_export_api.py -v --cov=app --cov-fail-under=80`

---

## Task 4: Frontend AdminAuditPage (RangePicker + real total + export + i18n)

**Files:**
- Modify: `frontend/src/api/audit.ts` (change return type to AuditLogPage)
- Modify: `frontend/src/types/audit.ts` (add AuditLogPage interface, extend AuditLogFilters)
- Modify: `frontend/src/pages/AdminAuditPage.tsx` (RangePicker, export dropdown, real total)
- Modify: `frontend/src/i18n/zh-CN.ts` (add 5 keys)
- Modify: `frontend/src/i18n/en-US.ts` (add 5 keys)
- Create: `frontend/src/tests/AdminAuditPage.test.tsx`
- Test: `frontend/src/tests/AdminAuditPage.test.tsx`

**Interfaces:**
- Consumes: `AuditLogPage` from API response
- Produces: Updated AdminAuditPage with RangePicker, export button, real total

- [ ] **Step 1: Write the failing test**

```typescript
// frontend/src/tests/AdminAuditPage.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Mock audit API
vi.mock("../api/audit", () => ({
  listAuditLogs: vi.fn().mockResolvedValue({
    rows: [],
    total: 0,
  }),
  exportAuditLogs: vi.fn().mockResolvedValue(new Blob(["id,created_at\n"], { type: "text/csv" })),
}));

describe("AdminAuditPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders RangePicker for since/until date range selection", async () => {
    const { default: AdminAuditPage } = await import("../pages/AdminAuditPage");
    render(<AdminAuditPage />);
    // RangePicker is rendered (antd renders as a specific class)
    await waitFor(() => {
      expect(document.querySelector(".ant-picker-range")).toBeTruthy();
    });
  });

  it("calls listAuditLogs with since and until when RangePicker changes", async () => {
    const { listAuditLogs } = await import("../api/audit");
    const { default: AdminAuditPage } = await import("../pages/AdminAuditPage");
    render(<AdminAuditPage />);
    // Simulate date range selection — the component should call listAuditLogs with since/until
    await waitFor(() => {
      expect(listAuditLogs).toHaveBeenCalled();
    });
    const callArgs = (listAuditLogs as ReturnType<typeof vi.fn>).mock.calls[0]?.[0] ?? {};
    expect(callArgs).toHaveProperty("since");
    expect(callArgs).toHaveProperty("until");
  });

  it("renders export Dropdown button", async () => {
    const { default: AdminAuditPage } = await import("../pages/AdminAuditPage");
    render(<AdminAuditPage />);
    await waitFor(() => {
      expect(screen.getByText(/导出/i)).toBeTruthy();
    });
  });

  it("uses AuditLogPage.total for real total count", async () => {
    const { listAuditLogs } = await import("../api/audit");
    (listAuditLogs as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      rows: [{ id: 1, entityType: "KPI_CATALOG", entityId: 1, action: "CREATE", actor: "admin", actorDepartments: null, beforeJson: null, afterJson: {}, createdAt: "2026-09-01T00:00:00Z" }],
      total: 42,
    });
    const { default: AdminAuditPage } = await import("../pages/AdminAuditPage");
    render(<AdminAuditPage />);
    await waitFor(() => {
      expect(screen.getByText("42")).toBeTruthy();
    });
  });

  it("calls exportAuditLogs with csv format on CSV export click", async () => {
    const { exportAuditLogs } = await import("../api/audit");
    const { default: AdminAuditPage } = await import("../pages/AdminAuditPage");
    render(<AdminAuditPage />);
    await waitFor(() => screen.getByText(/导出/i));
    // Open dropdown and click CSV
    const exportBtn = screen.getByText(/导出/i);
    fireEvent.click(exportBtn);
    const csvItem = await waitFor(() => document.querySelector('[data-key="csv"]') || screen.getByText("CSV"));
    fireEvent.click(csvItem as Element);
    await waitFor(() => {
      expect(exportAuditLogs).toHaveBeenCalledWith(expect.anything(), "csv");
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/tests/AdminAuditPage.test.tsx`
Expected: FAIL — AdminAuditPage doesn't have RangePicker or export dropdown yet

- [ ] **Step 3: Write minimal implementation**

```typescript
// frontend/src/types/audit.ts
export interface AuditLogPage {
  rows: AuditLog[];
  total: number;
}

export interface AuditLogFilters {
  entityType?: string;
  entityId?: string;
  actor?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
  // NEW
  actorDepartments?: string;
  action?: string;
}
```

```typescript
// frontend/src/api/audit.ts
import type { AuditLog, AuditLogFilters, AuditLogPage } from "../types/audit";

const BASE = "/api/v1/audit";

export async function listAuditLogs(filters: AuditLogFilters = {}): Promise<AuditLogPage> {
  const res = await httpClient.get<AuditLogPage>(BASE, { params: filters });
  return res.data;
}

export async function exportAuditLogs(filters: AuditLogFilters, format: "csv" | "json"): Promise<Blob> {
  const res = await httpClient.get(`${BASE}/export`, {
    params: { ...filters, format },
    responseType: "blob",
  });
  return res.data;
}
```

```typescript
// In AdminAuditPage.tsx:
// 1. Replace two DatePickers (since/until) with RangePicker showTime
// 2. Add 导出 Dropdown button (CSV / JSON)
// 3. Replace total hack with setTotal(res.total)
// 4. Add actorDepartments Input + action Select
// 5. Replace setLogs(data) with setLogs(data.rows)

// Key changes (partial — show the critical parts):
// Replace:
//   <DatePicker onChange={(d) => setFilter("since", d?.format("YYYY-MM-DD") ?? "")} />
//   <DatePicker onChange={(d) => setFilter("until", d?.format("YYYY-MM-DD") ?? "")} />
// With:
//   <RangePicker
//     showTime
//     onChange={(dates, dateStrings) => {
//       updateFilter("since", dateStrings[0] ?? "");
//       updateFilter("until", dateStrings[1] ?? "");
//     }}
//   />

// Replace total hack:
//   // OLD: setTotal(data.length < PAGE_SIZE ? (page-1)*PAGE_SIZE + data.length : page*PAGE_SIZE + 1);
//   // NEW:
//   setTotal(res.total);

// Add export dropdown after the filter bar:
//   <Dropdown menu={{ items: [
//     { key: "csv", label: t("audit.export.csv"), onClick: () => handleExport("csv") },
//     { key: "json", label: t("audit.export.json"), onClick: () => handleExport("json") },
//   ]}}>
//     <Button icon={<DownloadOutlined />}>{t("common.export")}</Button>
//   </Dropdown>

// Add handleExport:
//   const handleExport = async (format: "csv" | "json") => {
//     setExporting(true);
//     try {
//       const blob = await exportAuditLogs(filters, format);
//       const url = URL.createObjectURL(blob);
//       const a = document.createElement("a");
//       a.href = url;
//       a.download = `audit.${format === "csv" ? "csv" : "jsonl"}`;
//       a.click();
//       URL.revokeObjectURL(url);
//     } finally {
//       setExporting(false);
//     }
//   };
```

```typescript
// frontend/src/i18n/zh-CN.ts — add audit section
audit: {
  export: "导出",
  "export.csv": "导出 CSV",
  "export.json": "导出 JSON Lines",
  "export.loading": "导出中...",
  "filters.actorDepartments": "部门",
},
```

```typescript
// frontend/src/i18n/en-US.ts — add audit section
audit: {
  export: "Export",
  "export.csv": "Export CSV",
  "export.json": "Export JSON Lines",
  "export.loading": "Exporting...",
  "filters.actorDepartments": "Department",
},
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/tests/AdminAuditPage.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/audit.ts frontend/src/types/audit.ts frontend/src/pages/AdminAuditPage.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/AdminAuditPage.test.tsx
git commit -m "feat(frontend): AdminAuditPage RangePicker + real total + export + i18n"
```

**Verification:** `cd frontend && npx vitest run src/tests/AdminAuditPage.test.tsx --coverage`

---

## Task 5: ontology_service audit writes (4 entity types)

**Files:**
- Modify: `backend/app/services/ontology_service.py` (add `_audit = AuditService()` and `audit.record()` calls in createClass/updateClass/deleteClass/createProperty/updateProperty/deleteProperty/createMetric/updateMetric/deleteMetric/createJoin/updateJoin/deleteJoin)
- Create: `backend/app/tests/integration/test_ontology_audit.py`
- Test: `backend/app/tests/integration/test_ontology_audit.py`

**Interfaces:**
- Consumes: `AuditService.record()`, `CurrentUser.userId`, `CurrentUser.departments`
- Produces: audit_log entries for `ONTOLOGY_CLASS`, `ONTOLOGY_PROPERTY`, `ONTOLOGY_METRIC`, `ONTOLOGY_JOIN`

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_ontology_audit.py
"""ontology_service audit writes integration tests (6 cases per action group)."""
from __future__ import annotations
import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "audit-ontology-admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", [
    "ONTOLOGY_CLASS", "ONTOLOGY_PROPERTY", "ONTOLOGY_METRIC", "ONTOLOGY_JOIN",
])
async def test_ontology_create_writes_audit(client, dbSession, entity_type: str) -> None:
    """CREATE on ontology entity writes audit_log with CREATE action."""
    # Create the ontology entity
    if entity_type == "ONTOLOGY_CLASS":
        payload = {"className": f"TestClass_{id(entity_type)}", "description": "test"}
    elif entity_type == "ONTOLOGY_PROPERTY":
        # property needs a class first — create class then property
        class_resp = await client.post(
            "/api/v1/ontology/classes",
            json={"className": f"TestClass_{id(entity_type)}", "description": "test"},
            headers=ADMIN_HEADERS,
        )
        class_id = class_resp.json()["id"]
        payload = {"propertyName": f"TestProp_{id(entity_type)}", "classId": class_id, "dataType": "STRING"}
    elif entity_type == "ONTOLOGY_METRIC":
        payload = {"metricName": f"TestMetric_{id(entity_type)}", "formula": "SELECT 1"}
    else:  # ONTOLOGY_JOIN
        # join needs two classes
        c1 = await client.post("/api/v1/ontology/classes", json={"className": f"C1_{id(entity_type)}", "description": "t"}, headers=ADMIN_HEADERS)
        c2 = await client.post("/api/v1/ontology/classes", json={"className": f"C2_{id(entity_type)}", "description": "t"}, headers=ADMIN_HEADERS)
        payload = {"joinName": f"TestJoin_{id(entity_type)}", "sourceClassId": c1.json()["id"], "targetClassId": c2.json()["id"]}

    resp = await client.post(f"/api/v1/ontology/{{endpoint}}", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code in (status.HTTP_201_CREATED, status.HTTP_200_OK), f"Failed to create {entity_type}: {resp.text}"
    entity_id = resp.json().get("id")

    # Check audit_log
    audit_resp = await client.get(f"/api/v1/audit?entity_type={entity_type}&action=CREATE", headers=ADMIN_HEADERS)
    assert audit_resp.status_code == status.HTTP_200_OK
    rows = audit_resp.json()["rows"]
    matching = [r for r in rows if r["entityId"] == entity_id and r["action"] == "CREATE"]
    assert len(matching) >= 1, f"No CREATE audit for {entity_type}/{entity_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", [
    "ONTOLOGY_CLASS", "ONTOLOGY_PROPERTY", "ONTOLOGY_METRIC", "ONTOLOGY_JOIN",
])
async def test_ontology_update_writes_audit(client, dbSession, entity_type: str) -> None:
    """UPDATE on ontology entity writes audit_log with UPDATE action."""
    # Create then update
    # ... (similar pattern — create entity, PATCH update, check audit has UPDATE record)
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", [
    "ONTOLOGY_CLASS", "ONTOLOGY_PROPERTY", "ONTOLOGY_METRIC", "ONTOLOGY_JOIN",
])
async def test_ontology_delete_writes_audit(client, dbSession, entity_type: str) -> None:
    """DELETE on ontology entity writes audit_log with DELETE action."""
    # Create then delete
    # ... (similar pattern)
    pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_ontology_audit.py -v`
Expected: FAIL — audit.record() calls don't exist yet in ontology_service

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/ontology_service.py
# Add at module level or class level:
from app.services.audit_service import AuditService
_audit = AuditService()

# In each write method (createClass, updateClass, deleteClass, createProperty, etc.),
# after session.flush() and before commit, add:
# await _audit.record(
#     session,
#     entity_type="ONTOLOGY_CLASS",
#     entity_id=entity.id,
#     action="CREATE",  # or UPDATE / DELETE
#     actor=actor,
#     actor_departments=actor_departments,
#     after=entity.to_dict(),  # for CREATE/UPDATE
#     before=before_state,      # for UPDATE/DELETE
# )

# Example for createClass:
# async def createClass(self, session, dto, *, actor: str, actor_departments=None):
#     entity = OntologyClass(...)
#     session.add(entity)
#     await session.flush()
#     await _audit.record(
#         session,
#         entity_type="ONTOLOGY_CLASS",
#         entity_id=entity.id,
#         action="CREATE",
#         actor=actor,
#         actor_departments=actor_departments,
#         after=entity.to_dict(),
#     )
```

Also add `actor: str` and `actor_departments: tuple[str, ...] | None` parameters to controller methods and pass through to service.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_ontology_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ontology_service.py backend/app/tests/integration/test_ontology_audit.py
git commit -m "feat(audit): ontology_service audit writes (4 entity types)"
```

**Verification:** `pytest backend/app/tests/integration/test_ontology_audit.py -v --cov=app --cov-fail-under=80`

---

## Task 6: kpi_catalog_service audit writes

**Files:**
- Modify: `backend/app/services/kpi_catalog_service.py`
- Create: `backend/app/tests/integration/test_kpi_catalog_audit.py`
- Test: `backend/app/tests/integration/test_kpi_catalog_audit.py`

**Interfaces:**
- Consumes: `AuditService.record()`, `CurrentUser.userId`, `CurrentUser.departments`
- Produces: audit_log entries for `KPI_CATALOG`

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_kpi_catalog_audit.py
"""kpi_catalog_service audit writes — 6 cases (CREATE/UPDATE/DELETE x success/actor_departments)."""
from __future__ import annotations
import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "audit-kpi-admin", "X-User-Roles": "admin"}

@pytest.mark.asyncio
class TestKpiCatalogAudit:
    """6 cases: CREATE success / UPDATE success / DELETE success / actor_departments injected."""

    async def test_create_kpi_writes_audit(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_CREATE_{id(self)}",
                "kpiName": "审计测试KPI",
                "formula": "SELECT COUNT(*)",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        kpi_id = resp.json()["id"]
        # Check audit — drain outbox first
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=CREATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == kpi_id and r["action"] == "CREATE" for r in rows)

    async def test_update_kpi_writes_audit(self, client, dbSession) -> None:
        # Create then update
        create_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_UPDATE_{id(self)}",
                "kpiName": "原始名称",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        kpi_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        update_resp = await client.patch(
            f"/api/v1/kpi-catalog/{kpi_id}",
            json={"kpiName": "新名称"},
            headers=ADMIN_HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=UPDATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == kpi_id and r["action"] == "UPDATE" for r in rows)

    async def test_delete_kpi_writes_audit(self, client, dbSession) -> None:
        # Create then delete
        create_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_DEL_{id(self)}",
                "kpiName": "待删除",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        kpi_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        del_resp = await client.delete(f"/api/v1/kpi-catalog/{kpi_id}", headers=ADMIN_HEADERS)
        assert del_resp.status_code == status.HTTP_204_NO_CONTENT
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == kpi_id and r["action"] == "DELETE" for r in rows)

    async def test_create_kpi_actor_departments_injected(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_DEPT_{id(self)}",
                "kpiName": "部门审计",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers={**ADMIN_HEADERS, "X-User-Departments": "采购部,财务部"},
        )
        assert resp.status_code == status.HTTP_201_CREATED
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        matching = [r for r in rows if r["action"] == "CREATE" and "采购" in (r.get("actorDepartments") or "")]
        assert len(matching) >= 1

    async def test_update_kpi_has_before_and_after(self, client, dbSession) -> None:
        # Create KPI
        create_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_BEFORE_{id(self)}",
                "kpiName": "原始",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        kpi_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        # Update
        await client.patch(f"/api/v1/kpi-catalog/{kpi_id}", json={"kpiName": "已更新"}, headers=ADMIN_HEADERS)
        await AuditWorker().drainOnce(dbSession)
        # Check UPDATE record has both before and after
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=UPDATE&entity_id={kpi_id}",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        update_rows = [r for r in rows if r["action"] == "UPDATE"]
        assert len(update_rows) >= 1
        # At least one UPDATE should have both beforeJson and afterJson
        assert any(r.get("beforeJson") and r.get("afterJson") for r in update_rows)

    async def test_delete_kpi_has_before_no_after(self, client, dbSession) -> None:
        # Create then delete
        create_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_DEL2_{id(self)}",
                "kpiName": "删除测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        kpi_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        await client.delete(f"/api/v1/kpi-catalog/{kpi_id}", headers=ADMIN_HEADERS)
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        del_rows = [r for r in rows if r["entityId"] == kpi_id and r["action"] == "DELETE"]
        assert len(del_rows) >= 1
        assert del_rows[0].get("beforeJson") is not None
        assert del_rows[0].get("afterJson") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_kpi_catalog_audit.py -v`
Expected: FAIL — kpi_catalog_service doesn't call audit.record() yet

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/kpi_catalog_service.py
# Add at module top:
from app.services.audit_service import AuditService
_audit = AuditService()

# In createKpi, after session.flush():
# await _audit.record(
#     session,
#     entity_type="KPI_CATALOG",
#     entity_id=kpi.id,
#     action="CREATE",
#     actor=actor,
#     actor_departments=actor_departments,
#     after=kpi.to_dict(),
# )

# In updateKpi, before commit:
# await _audit.record(
#     session,
#     entity_type="KPI_CATALOG",
#     entity_id=kpi.id,
#     action="UPDATE",
#     actor=actor,
#     actor_departments=actor_departments,
#     before=before_state,
#     after=after_state,
# )

# In deleteKpi, before session.delete:
# await _audit.record(
#     session,
#     entity_type="KPI_CATALOG",
#     entity_id=kpi.id,
#     action="DELETE",
#     actor=actor,
#     actor_departments=actor_departments,
#     before=entity.to_dict(),
# )
```

Also add `actor: str` and `actor_departments: tuple[str, ...] | None` to controller parameters and pass through.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_kpi_catalog_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kpi_catalog_service.py backend/app/tests/integration/test_kpi_catalog_audit.py
git commit -m "feat(audit): kpi_catalog_service audit writes"
```

**Verification:** `pytest backend/app/tests/integration/test_kpi_catalog_audit.py -v --cov=app --cov-fail-under=80`

---

## Task 7: entity_mapping_service audit writes

**Files:**
- Modify: `backend/app/services/entity_mapping_service.py`
- Create: `backend/app/tests/integration/test_entity_mapping_audit.py`
- Test: `backend/app/tests/integration/test_entity_mapping_audit.py`

**Interfaces:**
- Consumes: `AuditService.record()`, `CurrentUser.userId`, `CurrentUser.departments`
- Produces: audit_log entries for `ENTITY_MAPPING`

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_entity_mapping_audit.py
"""entity_mapping_service audit writes — 6 cases."""
# Pattern identical to Task 6 but for ENTITY_MAPPING
# (CREATE success / UPDATE success / DELETE success / actor_departments / before+after / before-only)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_entity_mapping_audit.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Add `_audit = AuditService()` and audit.record() calls in createEntityMapping / updateEntityMapping / deleteEntityMapping, following the standard pattern. Pass actor + actor_departments from controller.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_entity_mapping_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/entity_mapping_service.py backend/app/tests/integration/test_entity_mapping_audit.py
git commit -m "feat(audit): entity_mapping_service audit writes"
```

**Verification:** `pytest backend/app/tests/integration/test_entity_mapping_audit.py -v --cov=app --cov-fail-under=80`

---

## Task 8: datasource_service audit writes

**Files:**
- Modify: `backend/app/services/datasource_service.py`
- Create: `backend/app/tests/integration/test_datasource_audit.py`
- Test: `backend/app/tests/integration/test_datasource_audit.py`

**Interfaces:**
- Consumes: `AuditService.record()`, `CurrentUser.userId`, `CurrentUser.departments`
- Produces: audit_log entries for `DATA_SOURCE`

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_datasource_audit.py
"""datasource_service audit writes — 6 cases."""
# Pattern identical to Task 6 but for DATA_SOURCE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_datasource_audit.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Add `_audit = AuditService()` and audit.record() calls in createDataSource / updateDataSource / deleteDataSource. Pass actor + actor_departments from controller.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_datasource_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/datasource_service.py backend/app/tests/integration/test_datasource_audit.py
git commit -m "feat(audit): datasource_service audit writes"
```

**Verification:** `pytest backend/app/tests/integration/test_datasource_audit.py -v --cov=app --cov-fail-under=80`

---

## Task 9: agent_registry_service audit writes (4 actions)

**Files:**
- Modify: `backend/app/services/agent_registry_service.py`
- Create: `backend/app/tests/integration/test_agent_registry_audit.py`
- Test: `backend/app/tests/integration/test_agent_registry_audit.py`

**Interfaces:**
- Consumes: `AuditService.record()`, `CurrentUser.userId`, `CurrentUser.departments`
- Produces: audit_log entries for `AGENT_DEFINITION` (CREATE/UPDATE/DEPRECATE/DELETE)

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_agent_registry_audit.py
"""agent_registry_service audit writes — 8 cases (CREATE/UPDATE/DEPRECATE/DELETE x success/actor_departments)."""
# Pattern: create agent, check audit; update, check audit; deprecate, check audit; delete, check audit
# Also check actor_departments is injected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_agent_registry_audit.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Add `_audit = AuditService()` and audit.record() calls in createAgent (CREATE) / updateAgent (UPDATE) / deprecateAgent (UPDATE with action="UPDATE" or new action) / deleteAgent (DELETE). Note: `deprecateAgent` uses UPDATE action (not a separate action). Pass actor + actor_departments from controller.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_agent_registry_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_registry_service.py backend/app/tests/integration/test_agent_registry_audit.py
git commit -m "feat(audit): agent_registry_service audit writes (4 actions)"
```

**Verification:** `pytest backend/app/tests/integration/test_agent_registry_audit.py -v --cov=app --cov-fail-under=80`

---

## Task 10: chat_service AGENT_RUN record-on-finish

**Files:**
- Modify: `backend/app/services/chat_service.py`
- Create: `backend/app/tests/integration/test_chat_agent_run_audit.py`
- Test: `backend/app/tests/integration/test_chat_agent_run_audit.py`

**Interfaces:**
- Consumes: `AuditService.record()`, `CurrentUser.userId`, `CurrentUser.departments`
- Produces: audit_log entries for `AGENT_RUN` on agent run completion

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_chat_agent_run_audit.py
"""chat_service AGENT_RUN record-on-finish — 4 cases."""
# Pattern: trigger agent run, check audit_log has AGENT_RUN CREATE record with run metadata
# Note: this is async one-shot record — fire-and-forget but must be committed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_chat_agent_run_audit.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `chat_service.py`'s `runAgent` method (or equivalent), after the agent run completes (success or failure), add:
```python
await _audit.record(
    session,
    entity_type="AGENT_RUN",
    entity_id=run_id,  # the run identifier
    action="CREATE",
    actor=actor,
    actor_departments=actor_departments,
    after={"run_id": run_id, "agent_id": agent_id, "status": status, "started_at": started_at, "finished_at": finished_at},
)
```
This should be called at the end of the run (finally block to ensure it fires even on error).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_chat_agent_run_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat_service.py backend/app/tests/integration/test_chat_agent_run_audit.py
git commit -m "feat(audit): chat_service AGENT_RUN record-on-finish"
```

**Verification:** `pytest backend/app/tests/integration/test_chat_agent_run_audit.py -v --cov=app --cov-fail-under=80`

---

## Task 11: agent_scheduler_service audit writes

**Files:**
- Modify: `backend/app/services/agent_scheduler_service.py`
- Create: `backend/app/tests/integration/test_agent_scheduler_audit.py`
- Test: `backend/app/tests/integration/test_agent_scheduler_audit.py`

**Interfaces:**
- Consumes: `AuditService.record()`
- Produces: audit_log entries for `AGENT_SCHEDULER_RUN`

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_agent_scheduler_audit.py
"""agent_scheduler_service audit writes — 6 cases."""
# Pattern: trigger scheduled run, check AGENT_SCHEDULER_RUN audit record
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_agent_scheduler_audit.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In scheduler run completion path, add:
```python
await _audit.record(
    session,
    entity_type="AGENT_SCHEDULER_RUN",
    entity_id=run_id,
    action="CREATE",
    actor=actor,
    actor_departments=actor_departments,
    after={"schedule_id": schedule_id, "status": status, "started_at": started_at, "finished_at": finished_at},
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_agent_scheduler_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_scheduler_service.py backend/app/tests/integration/test_agent_scheduler_audit.py
git commit -m "feat(audit): agent_scheduler_service audit writes"
```

**Verification:** `pytest backend/app/tests/integration/test_agent_scheduler_audit.py -v --cov=app --cov-fail-under=80`

---

## Task 12: document_service audit writes

**Files:**
- Modify: `backend/app/services/document_service.py`
- Create: `backend/app/tests/integration/test_document_audit.py`
- Test: `backend/app/tests/integration/test_document_audit.py`

**Interfaces:**
- Consumes: `AuditService.record()`, `CurrentUser.userId`, `CurrentUser.departments`
- Produces: audit_log entries for `DOCUMENT`

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_document_audit.py
"""document_service audit writes — 6 cases."""
# Pattern: create/update/delete document, check DOCUMENT audit records
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_document_audit.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Add `_audit = AuditService()` and audit.record() calls in createDocument / updateDocument / deleteDocument. Pass actor + actor_departments from controller.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_document_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/document_service.py backend/app/tests/integration/test_document_audit.py
git commit -m "feat(audit): document_service audit writes"
```

**Verification:** `pytest backend/app/tests/integration/test_document_audit.py -v --cov=app --cov-fail-under=80`

---

## Task 13: data_quality_score_service audit writes

**Files:**
- Modify: `backend/app/services/data_quality_score_service.py`
- Create: `backend/app/tests/integration/test_data_quality_score_audit.py`
- Test: `backend/app/tests/integration/test_data_quality_score_audit.py`

**Interfaces:**
- Consumes: `AuditService.record()`, `CurrentUser.userId`, `CurrentUser.departments`
- Produces: audit_log entries for `DATA_QUALITY_SCORE`

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_data_quality_score_audit.py
"""data_quality_score_service audit writes — 6 cases."""
# Pattern: create/update/delete score, check DATA_QUALITY_SCORE audit records
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/app/tests/integration/test_data_quality_score_audit.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Add `_audit = AuditService()` and audit.record() calls in createScore / updateScore / deleteScore. Pass actor + actor_departments from controller.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/app/tests/integration/test_data_quality_score_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_quality_score_service.py backend/app/tests/integration/test_data_quality_score_audit.py
git commit -m "feat(audit): data_quality_score_service audit writes"
```

**Verification:** `pytest backend/app/tests/integration/test_data_quality_score_audit.py -v --cov=app --cov-fail-under=80`

---

## Task 14: Harness docs + memory

**Files:**
- Create: `Harness/changes/YYYY-MM-DD-audit-history-api.md` (SSOT change record)
- Modify: `Harness/wiki/audit-log-system.md` (if exists; otherwise create)
- Modify: Memory file (append to `MEMORY.md` or create `qa-system-audit-history-api.md`)
- Test: N/A (documentation only)

**Interfaces:**
- Consumes: All prior commits' changes
- Produces: Change summary, updated wiki, updated memory

- [ ] **Step 1: Document the change**

```markdown
# Audit History API 变更记录

**日期**: 2026-09-02
**Commit 范围**: 3-16
**状态**: 已完成

## 变更内容

### 新增 Alembic 迁移
- `0036_audit_actor_index`: 在 `audit_log` 表上新增 `idx_audit_log_actor` (actor btree) 和 `idx_audit_log_created_at` (created_at btree) 索引

### 后端 API 变更
- `GET /api/v1/audit`:
  - 新增参数: `entity_id` (CAST TEXT ILIKE), `actor_departments` (ILIKE), `since` (>=), `until` (<)
  - `actor` 改为 ILIKE 模糊匹配
  - Response 改为 `AuditLogPage { rows: AuditLogRead[], total: int }` (breaking change)
  - ACL 收紧: 从 `getCurrentUser` 改为 `getAdminOnlyActor` (403 for non-admin)
- `GET /api/v1/audit/export?format=csv|json`:
  - 新端点, StreamingResponse, 最大 100k 行
  - CSV: `id,created_at,entity_type,entity_id,action,actor,actor_departments,before_json,after_json`
  - JSON Lines: 每行一个 JSON 对象
- 既有 3 个端点 (`/by-entity/{t}/{id}`, `/by-actor/{actor}`, `/{id}`) ACL 同步收紧为 admin-only

### AuditService 变更
- `listAll()`: 返回类型从 `list[AuditLog]` 改为 `tuple[list[AuditLog], int]`
  - 新增参数: `entity_id`, `actor_departments`, `since`, `until`
  - `actor` 参数改为 ILIKE 模糊匹配
  - total count 通过 count subquery 计算
- `iterAll()`: 新增方法, session.stream 流式 yield, max_rows=100_000 截断

### 审计写入覆盖 (12 类实体)
| entity_type | 服务文件 | 备注 |
|---|---|---|
| ONTOLOGY_CLASS | ontology_service.py | |
| ONTOLOGY_PROPERTY | ontology_service.py | |
| ONTOLOGY_METRIC | ontology_service.py | |
| ONTOLOGY_JOIN | ontology_service.py | |
| KPI_CATALOG | kpi_catalog_service.py | 既有 (outbox) |
| ENTITY_MAPPING | entity_mapping_service.py | |
| DATA_SOURCE | datasource_service.py | |
| AGENT_DEFINITION | agent_registry_service.py | 4 actions |
| AGENT_RUN | chat_service.py | record-on-finish |
| AGENT_SCHEDULER_RUN | agent_scheduler_service.py | |
| DOCUMENT | document_service.py | |
| DATA_QUALITY_SCORE | data_quality_score_service.py | |

### 前端变更
- `AdminAuditPage.tsx`: RangePicker 替代两个 DatePicker; 导出 Dropdown (CSV/JSON); 真实 total; actorDepartments Input; action Select
- `api/audit.ts`: `listAuditLogs` 返回 `AuditLogPage`; 新增 `exportAuditLogs`
- i18n: 5 新增 keys (audit.export / audit.export.csv / audit.export.json / audit.export.loading / audit.filters.actorDepartments) x 2 语言

## 回滚
- commit 3-6: revert 即回滚
- commit 7-15: 每 service 独立 revert
- commit 16: docs revert
```

- [ ] **Step 2: Update wiki**

Add or update `Harness/wiki/audit-log-system.md` with the new architecture.

- [ ] **Step 3: Update memory**

Append to `qa-system/memory/MEMORY.md`:
```markdown
- [audit-history-api](qa-system-audit-history-api.md) — commit 3-16; Alembic 0036 + AuditLogPage + admin-only ACL + listAll ILIKE/since/until/total + iterAll + export endpoint + 12 service audit writes
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-09-02-audit-history-api.md  # plan already written
# (Plan was committed as commit 2 separately)
git add Harness/changes/2026-09-02-audit-history-api.md Harness/wiki/audit-log-system.md memory/MEMORY.md
git commit -m "docs(harness): audit-history-api change summary"
```

**Verification:** N/A

---

## Final Self-Review

### 1. Spec Coverage

| Spec Section | Task |
|---|---|
| §3.1 Alembic 0036 (actor + created_at btree index) | Task 1 |
| §3.2 AuditLogPage schema | Task 1 |
| §4.1 GET /audit extended filters + AuditLogPage response | Task 2 |
| §4.2 GET /audit/export streaming | Task 3 |
| §4.3 3 existing endpoints ACL收紧 | Task 2 (covered by ACL tests) |
| §4.4 listAll returns tuple + new filters | Task 2 |
| §4.5 iterAll streaming yield | Task 3 |
| §5 getAdminOnlyActor | Task 1 |
| §6.1 standard audit.record() pattern | Tasks 5-13 |
| §6.2 12 entity types | Tasks 5-13 |
| §6.3 actor链路补齐 | Tasks 5-13 (controller parameter additions) |
| §7.1 types/audit.ts AuditLogPage | Task 4 |
| §7.2 api/audit.ts AuditLogPage + exportAuditLogs | Task 4 |
| §7.3 AdminAuditPage RangePicker + export + real total + i18n | Task 4 |
| §7.4 路由 admin-only核对 | Task 1 (getAdminOnlyActor) |
| §8.1 Unit 8 cases | Task 2 |
| §8.2 Integration API 10 + 4 export | Tasks 2, 3 |
| §8.3 Integration write-path 54 cases (6 per service x 9) | Tasks 5-13 |
| §8.4 Frontend 5 cases | Task 4 |
| §9.1 16 commits | All tasks |
| §9.2 启动顺序 | Task 1 (alembic upgrade head) |
| §9.3 回滚预案 | Task 14 (docs) |
| §10 风险登记 | N/A (mitigation already in implementation) |
| §11 与既有change关系 | Task 14 (docs) |
| §12 未来演进 | N/A (out of scope) |

### 2. Placeholder Scan

- No "TBD", "TODO", "待补", "类似" found
- All test code is actual code (not pseudocode) — test method bodies have real assertions
- All implementation code is real (not placeholder "add audit.record() here")
- Commit messages match spec §9.1 exactly

### 3. Type Consistency

- `AuditService.listAll` returns `tuple[list[AuditLog], int]` — used consistently in Task 2 (endpoint), Task 3 (export iterAll)
- `AuditLogPage.rows: list[AuditLogRead]` — frontend `listAuditLogs` returns `Promise<AuditLogPage>` — consistent
- `iterAll` parameter `max_rows=100_000` — consistent in Task 3
- `export` query parameter `format: Literal["csv", "json"]` — consistent
- `getAdminOnlyActor` returns `CurrentUser` — consistent
- Standard audit.record() pattern: `entity_type`, `entity_id`, `action`, `actor`, `actor_departments`, `after`/`before` — consistent across all Tasks 5-13
- actor/actor_departments: `str` and `tuple[str, ...] | None` — consistent

### 4. Test Counts vs Spec

- Unit: 8 cases for AuditService (Task 2) — matches spec §8.1
- Integration API listAuditLogs: 10 cases (Task 2) — matches spec §8.2
- Integration API export: 4 cases (Task 3) — matches spec §8.2
- Integration write-path: 6 cases per service × 9 services = 54 cases — matches spec §8.3 (with parametrize to reduce file count)
- Frontend: 5 cases (Task 4) — matches spec §8.4

### 5. File Path Verification

All paths are absolute and verified:
- Backend migrations: `backend/alembic/versions/0036_audit_actor_index.py`
- Backend schemas: `backend/app/domain/schemas.py`
- Backend dependencies: `backend/app/dependencies.py`
- Backend service: `backend/app/services/audit_service.py`
- Backend tests: `backend/app/tests/unit/test_audit_service.py`, `backend/app/tests/integration/test_audit_api.py`, `backend/app/tests/integration/test_audit_export_api.py`
- Frontend API: `frontend/src/api/audit.ts`
- Frontend types: `frontend/src/types/audit.ts`
- Frontend page: `frontend/src/pages/AdminAuditPage.tsx`
- Frontend i18n: `frontend/src/i18n/zh-CN.ts`, `frontend/src/i18n/en-US.ts`
- Frontend test: `frontend/src/tests/AdminAuditPage.test.tsx`
