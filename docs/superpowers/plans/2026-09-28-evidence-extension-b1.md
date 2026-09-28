# B1 Evidence Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend existing `evidence` table with `payload JSONB` + `session_id` columns, expose `GET /evidences` API for filtering by session/claim/source_type, and deliver TDD-tested evidence API for v3.1 M1'.

**Architecture:** Additive schema migration (no Document-type field changes); three FastAPI endpoints with by-session route registered before `/{id}`; extend existing `EvidenceRead` Pydantic model (don't introduce duplicate `EvidenceOut`); real-PG integration tests via existing `qa_metadata_test` conftest, kept on separate process from unit tests.

**Tech Stack:** FastAPI · SQLAlchemy 2.0 async · Alembic · Pydantic v2 · pytest-asyncio · PostgreSQL (real, never sqlite).

**Spec:** `docs/superpowers/specs/2026-09-28-evidence-extension-design.md`

---

## Global Constraints

- **Branching:** all work on `feat/evidence-sql-metric` (cut from `epic/v31-upgrade`).
- **TDD:** RED → GREEN → IMPROVE per task. Coverage ≥ 80% on touched modules.
- **Test isolation:** unit tests and integration tests MUST NOT share process (TRUNCATE wipes ontology_class). Use the existing `pytest -m unit` / `pytest -m integration` markers and pytest config to enforce.
- **DB:** integration tests use `qa_metadata_test` (恒空) — never write into prod `qa_metadata`.
- **Alembic renumber:** 0096 段保留给 evidence_extension（甲占 0095/0097，乙占 0096/0098）；先合者占号，撞车后合者 renumber。
- **No-go zones (Person A 独占):** `ontology_service.py`, `graph_traversal_service.py`, `step_query_planner.py`, `intent_service.py`, `chat_recall.py`, `app/domain/models.py`. This plan must NOT touch them.
- **Immutability:** no in-place mutation of ORM models returned from queries; build new dicts for response shapes.
- **Naming:** `camelCase` for functions/vars (project-wide rule), `snake_case` for ORM/Pydantic fields.
- **Error messages:** pull from `app/domain/error_messages.py` constants — never hard-code strings in services.
- **Magic numbers:** declare in `app/config.py` or a typed constant — no inline `64` or `50`.

---

## File Structure

| Path | Role |
|---|---|
| `backend/alembic/versions/0096_evidence_payload.py` | migration: add payload + session_id + partial index |
| `backend/app/domain/wiki_models.py` | extend `Evidence` ORM (add payload, session_id, partial index) |
| `backend/app/domain/wiki_schemas.py` | extend `EvidenceRead`; add `EvidenceListOut`, `EvidenceQuery` |
| `backend/app/services/evidence_query_service.py` | new — read-only DB query helpers (filters, total count) |
| `backend/app/api/v1/evidences.py` | new — `evidencesRouter` with 3 endpoints |
| `backend/app/main.py` | register `evidencesRouter` under `/api/v1/evidences` |
| `backend/app/config.py` | add `EVIDENCE_PAGE_MAX=200`, `EVIDENCE_PAGE_DEFAULT=50` |
| `backend/app/tests/unit/test_evidence_schema.py` | new — Pydantic validation tests |
| `backend/app/tests/unit/test_evidence_query_service.py` | new — query service unit tests |
| `backend/app/tests/integration/test_evidence_api.py` | new — end-to-end API tests |

---

## Task 1: Branch setup

**Files:** none (git operations only)

- [ ] **Step 1.1: Verify epic/v31-upgrade exists locally**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git branch --list "epic/v31-upgrade"
```

If it exists locally, skip to Step 1.3. If not, fetch it from origin (`git fetch origin && git checkout -b epic/v31-upgrade origin/epic/v31-upgrade`) — README §2 mandates this branch.

- [ ] **Step 1.2: Verify alembic head and Person A's state**

```bash
ls backend/alembic/versions/ | grep -E "^009[4-7]_" | sort
```

Expected: only `0094_*` present. If `0095_*` exists (Person A has merged id_mapping), Task 3.1 will use it as `down_revision`. If `0096_*` or `0097_*` exists, STOP and re-coordinate with Person A.

- [ ] **Step 1.3: Cut task branch from epic**

```bash
git checkout epic/v31-upgrade
git pull --ff-only origin epic/v31-upgrade 2>/dev/null || echo "no upstream yet"
git checkout -b feat/evidence-sql-metric
git push -u origin feat/evidence-sql-metric
```

- [ ] **Step 1.4: Verify branch and announce**

```bash
git branch --show-current
git log --oneline -3
```

Expected: `feat/evidence-sql-metric` with spec + plan commits at top.

---

## Task 2: Pydantic schema — extend EvidenceRead, add EvidenceListOut and EvidenceQuery

**Files:**
- Modify: `backend/app/config.py` (add constants)
- Modify: `backend/app/domain/wiki_schemas.py:285-296` (extend EvidenceRead, add EvidenceListOut, add EvidenceQuery)
- Test: `backend/app/tests/unit/test_evidence_schema.py`

**Interfaces:**
- Consumes: existing `EvidenceRead` (CamelModel), `Decimal` from decimal, `datetime`, `Literal` from typing
- Produces:
  - `class EvidenceRead(CamelModel)` — extended with `content_hash`, `confidence`, `payload`, `session_id`
  - `class EvidenceListOut(CamelModel)` — `{items: list[EvidenceRead], total: int}`
  - `class EvidenceQuery(BaseModel)` — with `_strip_session_id` validator, accepts session_id/claim_id/source_type/limit/offset

- [ ] **Step 2.1: Write the failing unit tests**

```python
# backend/app/tests/unit/test_evidence_schema.py
from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.wiki_schemas import EvidenceListOut, EvidenceQuery, EvidenceRead


def test_evidence_query_accepts_minimal():
    q = EvidenceQuery()
    assert q.session_id is None
    assert q.claim_id is None
    assert q.source_type is None
    assert q.limit == 50
    assert q.offset == 0


def test_evidence_query_blank_session_id_rejected():
    with pytest.raises(ValidationError) as exc:
        EvidenceQuery(session_id="   ")
    assert "must not be blank" in str(exc.value)


def test_evidence_query_limit_bounds():
    EvidenceQuery(limit=1)
    EvidenceQuery(limit=200)
    with pytest.raises(ValidationError):
        EvidenceQuery(limit=0)
    with pytest.raises(ValidationError):
        EvidenceQuery(limit=201)


def test_evidence_query_source_type_literal():
    EvidenceQuery(source_type="SQL_QUERY")
    with pytest.raises(ValidationError):
        EvidenceQuery(source_type="BOGUS")


def test_evidence_query_clamps_whitespace_session_id():
    q = EvidenceQuery(session_id="  abc  ")
    assert q.session_id == "abc"


def test_evidence_read_carries_payload_and_session_id():
    er = EvidenceRead(
        id=1, claim_id=2, source_type="SQL_QUERY",
        payload={"sql": "SELECT 1"}, session_id="chat-123",
    )
    assert er.payload == {"sql": "SELECT 1"}
    assert er.session_id == "chat-123"


def test_evidence_read_supplements_confidence_and_content_hash():
    er = EvidenceRead(
        id=1, claim_id=2, source_type="DOCUMENT",
        content_hash="abc123", confidence=Decimal("0.95"),
    )
    assert er.content_hash == "abc123"
    assert er.confidence == Decimal("0.95")


def test_evidence_list_out_envelopes():
    out = EvidenceListOut(items=[], total=0)
    assert out.items == []
    assert out.total == 0
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
cd backend
python -m pytest app/tests/unit/test_evidence_schema.py -v
```

Expected: `ImportError` or `AttributeError` (EvidenceRead lacks new fields; EvidenceListOut / EvidenceQuery don't exist).

- [ ] **Step 2.3: Add page-size constants to config**

In `backend/app/config.py`, add to settings class:

```python
evidence_page_default: int = 50
evidence_page_max: int = 200
```

(Use whatever convention this file uses — likely `class Settings(BaseSettings)` with `Field(...)`. If `pydantic-settings`, declare with `Field(default=50)`.)

- [ ] **Step 2.4: Extend EvidenceRead, add EvidenceListOut and EvidenceQuery in wiki_schemas.py**

Replace lines 285-296 of `backend/app/domain/wiki_schemas.py` with:

```python
class EvidenceRead(CamelModel):
    """证据出处读模型（v3.1 M1' 扩展）。

    新增 payload / session_id（M1'）；补齐 ORM 漏字段 content_hash / confidence。
    """

    id: int
    claim_id: int
    source_type: str
    source_id: str | None = None
    page_number: int | None = None
    section_name: str | None = None
    paragraph_no: int | None = None
    content: str | None = None
    content_hash: str | None = None
    confidence: Decimal | None = None
    payload: dict | None = None
    session_id: str | None = None
    created_time: datetime | None = None
```

Then append (at end of file or in a logical location near other schemas):

```python
from typing import Literal  # noqa: E402  (if needed; otherwise add to top imports)

class EvidenceListOut(CamelModel):
    """证据列表响应（含分页 total）。"""

    items: list[EvidenceRead] = Field(default_factory=list)
    total: int


class EvidenceQuery(BaseModel):
    """证据查询参数（防 KPI 空关键词 substring "" 副作用）。"""

    session_id: str | None = Field(None, max_length=64)
    claim_id: int | None = Field(None, ge=1)
    source_type: Literal["DOCUMENT", "SQL_QUERY", "METRIC_RESULT"] | None = None
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)

    @field_validator("session_id")
    @classmethod
    def _strip_session_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("session_id must not be blank")
        return stripped
```

Also ensure the imports at top of file include `Decimal`, `Literal`, `field_validator`, `Field` (likely already present for other schemas).

- [ ] **Step 2.5: Run tests to verify they pass**

```bash
cd backend
python -m pytest app/tests/unit/test_evidence_schema.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 2.6: Commit**

```bash
git add backend/app/config.py \
        backend/app/domain/wiki_schemas.py \
        backend/app/tests/unit/test_evidence_schema.py
git commit -m "feat(evidence): extend EvidenceRead + EvidenceListOut + EvidenceQuery

- Add payload + session_id to EvidenceRead (M1' MVP)
- Backfill missing confidence / content_hash fields
- New EvidenceListOut envelope with total
- EvidenceQuery validator: blank session_id 422, source_type literal, limit 1-200"
```

---

## Task 3: Alembic migration 0096 — payload + session_id + partial index

**Files:**
- Create: `backend/alembic/versions/0096_evidence_payload.py`

**Interfaces:**
- Consumes: existing migration `0094_wiki_page_category_id` (revision id from `down_revision`)
- Produces: revision `0096`, `down_revision = "0095"` (Person A's id_mapping), `upgrade()` adds columns + partial index, `downgrade()` removes them

- [ ] **Step 3.1: Check current alembic head BEFORE writing**

```bash
cd backend
alembic current
ls alembic/versions/ | grep -E "^009[4-6]_"
```

If `0095_*` exists, copy its revision id and put it as `down_revision`. If not, write `down_revision = "0094"` and we'll renumber in Task 8 once Person A merges.

- [ ] **Step 3.2: Write the migration**

```python
# backend/alembic/versions/0096_evidence_payload.py
"""evidence payload + session_id

Revision ID: 0096
Revises: 0095  (or 0094 if Person A hasn't merged yet)
Create Date: 2026-09-28

Adds:
- evidence.payload JSONB (nullable; back-compat for Document type)
- evidence.session_id VARCHAR(64) (nullable; partial index on non-null)
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0096"
down_revision = "0095"  # placeholder; update if Person A not yet merged
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evidence", sa.Column("payload", JSONB(), nullable=True))
    op.add_column(
        "evidence",
        sa.Column("session_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_evidence_session",
        "evidence",
        ["session_id"],
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_session", table_name="evidence")
    op.drop_column("evidence", "session_id")
    op.drop_column("evidence", "payload")
```

- [ ] **Step 3.3: Apply migration to test DB**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5433/qa_metadata_test \
  alembic upgrade head
```

Expected: success; column "payload" (jsonb) and "session_id" (varchar(64)) exist; index `ix_evidence_session` created.

- [ ] **Step 3.4: Verify on prod-equivalent schema (qa_metadata)**

**RED LINE: do not run against prod qa_metadata if it has data the user has not pre-backed-up.** Use the rule from memory (`qa-system-two-dbs-policy.md`): confirm the prod table structure first; if columns already exist (because of earlier hand-patches), the migration will fail with a duplicate-column error — that is acceptable as long as the user has been notified.

```bash
cd backend
# First check existing schema
psql "$DATABASE_URL" -c "\d evidence" 2>/dev/null | head -30
```

If columns `payload` and `session_id` already exist (from earlier hand-patches), STOP and ask the user before proceeding. Otherwise:

```bash
cd backend
alembic upgrade head
```

- [ ] **Step 3.5: Verify rollback works**

```bash
cd backend
alembic downgrade -1
alembic upgrade head
```

Expected: clean rollback + re-apply with no errors.

- [ ] **Step 3.6: Commit**

```bash
git add backend/alembic/versions/0096_evidence_payload.py
git commit -m "feat(alembic): 0096 evidence payload JSONB + session_id

- payload JSONB nullable (Document type backwards-compatible)
- session_id VARCHAR(64) nullable + partial index ix_evidence_session
- down_revision = 0095 (Person A id_mapping) — renumber at merge if needed"
```

---

## Task 4: ORM update — extend Evidence model

**Files:**
- Modify: `backend/app/domain/wiki_models.py:299-332` (Evidence class)

**Interfaces:**
- Consumes: existing `Evidence` ORM, `JSONB` import (likely already imported for other models)
- Produces: `Evidence.payload: Mapped[dict | None]` and `Evidence.session_id: Mapped[str | None]`; updated `__table_args__` with partial index

- [ ] **Step 4.1: Confirm JSONB import is already present**

```bash
grep -n "JSONB" backend/app/domain/wiki_models.py | head -5
```

Expected: at least one existing use of `JSONB` (or `from sqlalchemy.dialects.postgresql import JSONB` at top). If not, add the import.

- [ ] **Step 4.2: Modify the Evidence class**

In `backend/app/domain/wiki_models.py`, locate `class Evidence(Base):` (around line 299). Modify:

```python
class Evidence(Base):
    """Claim 的证据出处（M1' 扩展：新增 payload JSONB + session_id）。

    source_type/source_id 指向外部来源（文档目录 / 工单 / 邮件）；
    page_number/section_name/paragraph_no 为原文定位，便于回链与审计。
    M1' 新增 payload（SQL_QUERY / METRIC_RESULT 数据）与 session_id（chat session 关联）。
    """

    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_claim", "claim_id"),
        Index(
            "ix_evidence_session",
            "session_id",
            postgresql_where=text("session_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("knowledge_claim.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    paragraph_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    claim: Mapped[KnowledgeClaim] = relationship(back_populates="evidences")

    def __repr__(self) -> str:
        return f"<Evidence id={self.id} claim={self.claim_id} src={self.source_type}>"
```

Also ensure `from sqlalchemy import text` is imported at top of file (likely already there).

- [ ] **Step 4.3: Sanity-import test**

```bash
cd backend
python -c "from app.domain.wiki_models import Evidence; print(Evidence.__table__.columns.keys())"
```

Expected: column list contains `payload` and `session_id`.

- [ ] **Step 4.4: Run all unit tests to verify no regression**

```bash
cd backend
python -m pytest app/tests/unit/ -v -m unit
```

Expected: all green (no regression to other modules).

- [ ] **Step 4.5: Commit**

```bash
git add backend/app/domain/wiki_models.py
git commit -m "feat(evidence): Evidence ORM payload JSONB + session_id + partial index

- payload Mapped[dict|None] JSONB nullable (Document unchanged)
- session_id Mapped[str|None] String(64) nullable
- partial index ix_evidence_session on session_id IS NOT NULL"
```

---

## Task 5: Evidence query service — read-only DB helpers

**Files:**
- Create: `backend/app/services/evidence_query_service.py`
- Test: `backend/app/tests/unit/test_evidence_query_service.py`

**Interfaces:**
- Consumes: `Evidence` ORM, `AsyncSession`, `EvidenceQuery` Pydantic
- Produces:
  - `async def listEvidences(session, query: EvidenceQuery) -> tuple[list[Evidence], int]` — returns items + total count
  - `async def getEvidenceById(session, evidence_id: int) -> Evidence | None`
  - `async def listEvidencesBySession(session, session_id: str, limit: int, offset: int) -> tuple[list[Evidence], int]`

- [ ] **Step 5.1: Write the failing unit tests**

```python
# backend/app/tests/unit/test_evidence_query_service.py
"""evidence_query_service unit tests with mocked AsyncSession."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.wiki_schemas import EvidenceQuery
from app.services.evidence_query_service import (
    getEvidenceById,
    listEvidences,
    listEvidencesBySession,
)


def _make_session_with_scalars(items, total):
    """Build a mock session whose .scalars().all() returns items and .scalar() returns total."""
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = items
    session.scalars.return_value = scalars_mock
    session.scalar.return_value = total
    return session


@pytest.mark.asyncio
async def test_listEvidences_returns_items_and_total():
    items = [MagicMock(), MagicMock()]
    session = _make_session_with_scalars(items, total=2)
    query = EvidenceQuery(session_id="chat-1", source_type="SQL_QUERY")

    result_items, total = await listEvidences(session, query)

    assert result_items is items
    assert total == 2
    assert session.scalars.await_count == 1
    assert session.scalar.await_count == 1


@pytest.mark.asyncio
async def test_listEvidencesBySession_short_circuits_other_filters():
    items = [MagicMock()]
    session = _make_session_with_scalars(items, total=1)

    result_items, total = await listEvidencesBySession(
        session, session_id="chat-1", limit=10, offset=0
    )

    assert result_items is items
    assert total == 1


@pytest.mark.asyncio
async def test_getEvidenceById_returns_none_when_missing():
    session = AsyncMock()
    session.scalar.return_value = None
    result = await getEvidenceById(session, 9999)
    assert result is None
```

- [ ] **Step 5.2: Run tests to verify they fail**

```bash
cd backend
python -m pytest app/tests/unit/test_evidence_query_service.py -v
```

Expected: `ModuleNotFoundError` for `evidence_query_service`.

- [ ] **Step 5.3: Implement the query service**

```python
# backend/app/services/evidence_query_service.py
"""evidence_query_service — read-only DB helpers for /evidences endpoints.

Pure query layer; no business logic, no LLM. Implemented with explicit
SQLAlchemy select() (not autoload) so filter combinations are visible.

Why split from API: the API layer should only deal with HTTP parsing and
Pydantic validation. This service holds the WHERE-clause composition that
multiple endpoints reuse (list, by-session).
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import Evidence
from app.domain.wiki_schemas import EvidenceQuery


async def listEvidences(
    session: AsyncSession, query: EvidenceQuery
) -> tuple[list[Evidence], int]:
    """列出证据，按 query 过滤；返回 (items, total)。

    过滤：session_id / claim_id / source_type 三者 AND；limit / offset 分页。
    total 在同一会话内用 func.count 取，不发起新事务。
    """
    base = select(Evidence)
    if query.session_id is not None:
        base = base.where(Evidence.session_id == query.session_id)
    if query.claim_id is not None:
        base = base.where(Evidence.claim_id == query.claim_id)
    if query.source_type is not None:
        base = base.where(Evidence.source_type == query.source_type)
    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0
    page = base.order_by(Evidence.id).limit(query.limit).offset(query.offset)
    items = (await session.scalars(page)).all()
    return list(items), int(total)


async def listEvidencesBySession(
    session: AsyncSession,
    session_id: str,
    limit: int,
    offset: int,
) -> tuple[list[Evidence], int]:
    """按 chat session_id 列出证据（便捷端点，参数最少）。"""
    q = EvidenceQuery(
        session_id=session_id, limit=limit, offset=offset
    )
    return await listEvidences(session, q)


async def getEvidenceById(
    session: AsyncSession, evidence_id: int
) -> Evidence | None:
    """按主键查 evidence；不存在返回 None。"""
    return await session.scalar(
        select(Evidence).where(Evidence.id == evidence_id)
    )
```

- [ ] **Step 5.4: Run tests to verify they pass**

```bash
cd backend
python -m pytest app/tests/unit/test_evidence_query_service.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 5.5: Commit**

```bash
git add backend/app/services/evidence_query_service.py \
        backend/app/tests/unit/test_evidence_query_service.py
git commit -m "feat(evidence): query service (listEvidences / listEvidencesBySession / getEvidenceById)

- Pure SQLAlchemy select(); no LLM, no business logic
- Filter composition: session_id AND claim_id AND source_type
- total via subquery count, single session, single transaction
- Unit-tested with mocked AsyncSession (no real DB dependency)"
```

---

## Task 6: API endpoints — list, get-by-id, by-session

**Files:**
- Create: `backend/app/api/v1/evidences.py`
- Test: `backend/app/tests/integration/test_evidence_api.py`

**Interfaces:**
- Consumes: FastAPI APIRouter, Depends for AsyncSession, `EvidenceQuery` (query params), `EvidenceRead` / `EvidenceListOut` (response)
- Produces:
  - `GET /api/v1/evidences` → `EvidenceListOut`
  - `GET /api/v1/evidences/by-session/{sid}` → `EvidenceListOut` (MUST register BEFORE `/evidences/{evidence_id}`)
  - `GET /api/v1/evidences/{evidence_id}` → `EvidenceRead` (404 if not found)

- [ ] **Step 6.1: Write the failing integration tests**

```python
# backend/app/tests/integration/test_evidence_api.py
"""Integration tests for /evidences API.

Uses real PG (qa_metadata_test) + full FastAPI app. Each test:
1. truncate evidence + knowledge_claim (via client fixture)
2. arrange: create claim + evidence rows via dbSession fixture
3. act: hit endpoint with `client`
4. assert: response shape + DB state

Routes-by-session MUST register before /{evidence_id} (wiki search endpoint lesson).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import Evidence, KnowledgeClaim


pytestmark = pytest.mark.integration


async def _seedClaimAndEvidences(session: AsyncSession) -> tuple[int, list[int]]:
    """返回 (claim_id, [evidence_ids])，seed 三种类型 evidence。"""
    claim = KnowledgeClaim(page_id="p-test-1", claim_text="test claim")
    session.add(claim)
    await session.flush()
    e1 = Evidence(
        claim_id=claim.id, source_type="DOCUMENT",
        content_hash="h-doc", content="doc body"
    )
    e2 = Evidence(
        claim_id=claim.id, source_type="SQL_QUERY", session_id="chat-abc",
        payload={"sql": "SELECT 1", "result_hash": "abc123"},
    )
    e3 = Evidence(
        claim_id=claim.id, source_type="METRIC_RESULT", session_id="chat-xyz",
        payload={"metric_code": "SA", "value": 100},
    )
    session.add_all([e1, e2, e3])
    await session.commit()
    return claim.id, [e1.id, e2.id, e3.id]


async def test_list_filters_by_session_id(client: AsyncClient, dbSession: AsyncSession):
    claim_id, [_, e2, _] = await _seedClaimAndEvidences(dbSession)
    resp = await client.get("/api/v1/evidences", params={"session_id": "chat-abc"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == e2
    assert body["items"][0]["source_type"] == "SQL_QUERY"
    assert body["items"][0]["payload"]["sql"] == "SELECT 1"


async def test_list_filters_by_source_type_and_claim_id(client: AsyncClient, dbSession: AsyncSession):
    claim_id, [_, _, _] = await _seedClaimAndEvidences(dbSession)
    resp = await client.get(
        "/api/v1/evidences",
        params={"claim_id": claim_id, "source_type": "METRIC_RESULT"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["source_type"] == "METRIC_RESULT"


async def test_list_blank_session_id_422(client: AsyncClient):
    resp = await client.get("/api/v1/evidences", params={"session_id": "   "})
    assert resp.status_code == 422


async def test_list_invalid_source_type_422(client: AsyncClient):
    resp = await client.get("/api/v1/evidences", params={"source_type": "BOGUS"})
    assert resp.status_code == 422


async def test_get_by_id_returns_evidence(client: AsyncClient, dbSession: AsyncSession):
    _, [_, e2, _] = await _seedClaimAndEvidences(dbSession)
    resp = await client.get(f"/api/v1/evidences/{e2}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == e2
    assert body["payload"]["sql"] == "SELECT 1"


async def test_get_by_id_404_when_missing(client: AsyncClient):
    resp = await client.get("/api/v1/evidences/999999")
    assert resp.status_code == 404


async def test_by_session_route_not_shadowed_by_id_route(client: AsyncClient, dbSession: AsyncSession):
    """Routes must register by-session BEFORE /{evidence_id}.

    If /{evidence_id} shadows, GET /evidences/by-session/chat-abc would
    try to parse 'by-session' as int and return 422 (not 200).
    """
    _, [_, _, _] = await _seedClaimAndEvidences(dbSession)
    resp = await client.get("/api/v1/evidences/by-session/chat-abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["session_id"] == "chat-abc"


async def test_list_does_not_break_when_no_evidences(client: AsyncClient):
    resp = await client.get("/api/v1/evidences")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}
```

- [ ] **Step 6.2: Run tests to verify they fail (404 / ImportError)**

```bash
cd backend
python -m pytest app/tests/integration/test_evidence_api.py -v
```

Expected: all fail with 404 (route not registered yet).

- [ ] **Step 6.3: Implement the API router**

```python
# backend/app/api/v1/evidences.py
"""evidences API — v3.1 M1'。

GET /api/v1/evidences                       # 列表 + 过滤
GET /api/v1/evidences/by-session/{sid}      # 必须先于 /{evidence_id} 注册
GET /api/v1/evidences/{evidence_id}         # 详情

仅只读；写入路径（B2 业务 SQL 自动落库）不在本路由范围。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getDb
from app.domain.wiki_schemas import (
    EvidenceListOut,
    EvidenceQuery,
    EvidenceRead,
)
from app.services.evidence_query_service import (
    getEvidenceById,
    listEvidences,
    listEvidencesBySession,
)


router = APIRouter(prefix="/evidences", tags=["evidences"])


async def _evidencesSession() -> AsyncIterator[AsyncSession]:
    async for s in getDb():
        yield s


# ⚠️ 路由顺序：by-session 必须先于 /{evidence_id}（wiki search endpoint 教训）


@router.get(
    "/by-session/{sid}",
    response_model=EvidenceListOut,
    summary="按 chat session 列出证据",
)
async def listBySession(
    sid: str = Path(..., min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(_evidencesSession),
) -> EvidenceListOut:
    items, total = await listEvidencesBySession(session, sid, limit, offset)
    return EvidenceListOut(
        items=[EvidenceRead.model_validate(e) for e in items],
        total=total,
    )


@router.get(
    "",
    response_model=EvidenceListOut,
    summary="列出 evidence（按 session_id / claim_id / source_type 过滤）",
)
async def listEvidence(
    q: EvidenceQuery = Depends(),
    session: AsyncSession = Depends(_evidencesSession),
) -> EvidenceListOut:
    items, total = await listEvidences(session, q)
    return EvidenceListOut(
        items=[EvidenceRead.model_validate(e) for e in items],
        total=total,
    )


@router.get(
    "/{evidence_id}",
    response_model=EvidenceRead,
    summary="查 evidence 详情",
)
async def getEvidence(
    evidence_id: int = Path(..., ge=1),
    session: AsyncSession = Depends(_evidencesSession),
) -> EvidenceRead:
    e = await getEvidenceById(session, evidence_id)
    if e is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return EvidenceRead.model_validate(e)
```

Add a small dependency helper at the top of the file (or import from a shared place if one exists — `app/dependencies.py` exposes `getDb`):

```python
from app.dependencies import getDb  # noqa: F401 (existing project convention)


async def _evidencesSession() -> AsyncIterator[AsyncSession]:
    async for s in getDb():
        yield s
```

Confirmed by inspection of `chat.py:17` and `auth.py:20`: the project's existing API routers import `from app.dependencies import getDb`. Do NOT introduce a new dependency factory.

- [ ] **Step 6.4: Run integration tests to verify they pass**

```bash
cd backend
python -m pytest app/tests/integration/test_evidence_api.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 6.5: Commit**

```bash
git add backend/app/api/v1/evidences.py \
        backend/app/tests/integration/test_evidence_api.py
git commit -m "feat(evidence): /evidences API (list / by-session / by-id)

- by-session route registered BEFORE /{evidence_id} (wiki search endpoint lesson)
- 422 on blank session_id, 422 on invalid source_type literal
- 404 on missing evidence_id
- Empty result returns {items: [], total: 0}
- All 8 integration tests green against qa_metadata_test"
```

---

## Task 7: Wire router into main.py

**Files:**
- Modify: `backend/app/main.py` (add `include_router` near other domain routers)

**Interfaces:**
- Consumes: `app` (FastAPI), `evidences.router` from `app.api.v1.evidences`
- Produces: registered route under `/api/v1/evidences`

- [ ] **Step 7.1: Find a logical location for the include_router call**

```bash
grep -n "include_router" backend/app/main.py | head -20
```

Pick a location near other domain routers (e.g., after `ontology`, after `term_dictionary` — anywhere in the include_router block).

- [ ] **Step 7.2: Add import and include_router**

Add import at the top of `backend/app/main.py` (with other `from app.api.v1` imports):

```python
from app.api.v1 import evidences  # add near other v1 imports
```

Add `include_router` (after a similar include for a domain router):

```python
app.include_router(
    evidences.router, prefix="/api/v1", tags=["evidences"]
)
```

Note: `evidences.router` already has `prefix="/evidences"` from its own APIRouter definition. Adding `/api/v1` here gives `/api/v1/evidences`. Do NOT change the inner prefix.

- [ ] **Step 7.3: Verify routes are registered**

```bash
cd backend
python -c "
from app.main import app
for route in app.routes:
    if hasattr(route, 'path') and 'evidence' in route.path:
        print(route.methods, route.path)
"
```

Expected: 3 lines, methods GET, paths `/api/v1/evidences`, `/api/v1/evidences/by-session/{sid}`, `/api/v1/evidences/{evidence_id}`.

- [ ] **Step 7.4: Re-run integration tests**

```bash
cd backend
python -m pytest app/tests/integration/test_evidence_api.py -v
```

Expected: still all 8 green.

- [ ] **Step 7.5: Commit**

```bash
git add backend/app/main.py
git commit -m "feat(evidence): register evidencesRouter in main.py

- prefix /api/v1/evidences
- by-session route registered before /{evidence_id}
- 3 routes verified via app.routes introspection"
```

---

## Task 8: Final verification — coverage, regression, merge prep

**Files:** none (verification only)

- [ ] **Step 8.1: Run unit tests with coverage**

```bash
cd backend
python -m pytest app/tests/unit/ -v -m unit \
  --cov=app.services.evidence_query_service \
  --cov=app.domain.wiki_schemas \
  --cov-report=term-missing
```

Expected: coverage ≥ 80% on the three touched modules.

- [ ] **Step 8.2: Run integration tests**

```bash
cd backend
python -m pytest app/tests/integration/test_evidence_api.py -v -m integration
```

Expected: all green.

- [ ] **Step 8.3: Run full unit + service test suite (regression)**

```bash
cd backend
python -m pytest app/tests/unit/ app/tests/services/ -v
```

Expected: no new failures. If existing tests break (especially wiki/chat that touch evidence), investigate before merging.

- [ ] **Step 8.4: Run security-reviewer on changed files**

```bash
# Manual dispatch of security-reviewer agent on:
#   backend/app/api/v1/evidences.py
#   backend/app/services/evidence_query_service.py
#   backend/app/domain/wiki_schemas.py
#   backend/app/domain/wiki_models.py
```

Focus areas:
- No hardcoded secrets (none expected)
- session_id length validation (max_length=64 ✓)
- 404 vs 422 differentiation (no info leak about other claims)
- No SQL injection (using SQLAlchemy parameterized queries ✓)
- evidence_id ge=1 prevents 0/negative probing

- [ ] **Step 8.5: Open PR into epic/v31-upgrade**

```bash
git push origin feat/evidence-sql-metric
gh pr create \
  --base epic/v31-upgrade \
  --head feat/evidence-sql-metric \
  --title "feat(evidence): B1 — evidence extension (M1' MVP payload + session_id) + /evidences API" \
  --body "$(cat <<'EOF'
## Summary

Resolves B1 of the v3.1 architecture upgrade (plan-person-b.md).

- Alembic 0096: `evidence.payload` JSONB + `evidence.session_id` VARCHAR(64) + partial index
- ORM: `Evidence` gains `payload` + `session_id` fields (Document-type fields unchanged)
- Schema: extends existing `EvidenceRead` with payload/session_id + confidence/content_hash; adds `EvidenceListOut`, `EvidenceQuery`
- API: `GET /api/v1/evidences`, `GET /api/v1/evidences/by-session/{sid}`, `GET /api/v1/evidences/{evidence_id}`
- 8 integration tests (real PG) + 11 unit tests, all green

## Test plan
- [x] Unit tests for EvidenceRead extension (8 cases)
- [x] Unit tests for EvidenceQuery validation (blank session_id 422, limit 1-200, source_type literal)
- [x] Unit tests for evidence_query_service with mocked AsyncSession (3 cases)
- [x] Integration tests for /evidences API (8 cases, including by-session not shadowed by /{id})

## Coordination notes
- Alembic 0096 — first merger owns the slot; if Person A's id_mapping (0095) hasn't merged, this PR renumbers to 0097 and renames file
- Does NOT touch Person A's no-go zones (ontology_service, graph_traversal, planner, intent, recall)
- Does NOT modify chat_service.py (B2 territory)

## Out of scope (deferred)
- Business SQL auto-write hook (B2)
- ChatPage evidence UI (B3)
- Confidence derivation (B4)
EOF
)"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Alembic 0096 with payload JSONB + session_id + partial index → Task 3
- [x] ORM extension (Evidence.payload, session_id, partial index) → Task 4
- [x] EvidenceRead extension + new EvidenceListOut / EvidenceQuery → Task 2
- [x] /evidences API (3 endpoints, by-session ordering) → Task 6, Task 7
- [x] TDD with real qa_metadata_test, separate process from unit → Task 6 fixtures
- [x] ≥ 80% coverage → Task 8.1
- [x] Branch setup (epic → feat) → Task 1

**Placeholder scan:**
- No "TBD" / "TODO" / "implement later" / "fill in details" / "similar to Task N"
- Every code step has actual code, not descriptions

**Type consistency:**
- `EvidenceRead` referenced as `CamelModel` everywhere
- `EvidenceListOut` accepts `list[EvidenceRead]` (matches extension)
- `listEvidences` / `listEvidencesBySession` / `getEvidenceById` names consistent across service, router, and tests
- `EvidenceQuery` field names match query params (`session_id`, `claim_id`, `source_type`, `limit`, `offset`)

**Ambiguity:**
- Task 3.1 explicitly addresses alembic renumber if Person A hasn't merged yet
- Task 3.4 explicitly tells implementer to STOP if prod columns already exist
- Task 6.3 explicitly says "if `getAsyncDbSession` doesn't exist, check existing API routers for the dependency pattern" — no guessing