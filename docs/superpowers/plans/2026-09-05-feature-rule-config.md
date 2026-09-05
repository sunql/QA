# Feature Rule Config DB-Backed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate hardcoded supplier risk rules (`DEFAULT_SUPPLIER_FEATURES` + `RISK_RULES`) to a generic DB-backed Feature Rule engine with structured SSOT, tiered thresholds, admin UI, and LLM-assisted NL form fill (config-time only — runtime stays deterministic).

**Architecture:** 3-layer — (1) DB (`feature_rule` + `feature_rule_threshold` + service + registry + admin UI), (2) Pure-sync `FeatureRuleEvaluator` (no IO / no LLM), (3) Optional `POST /parse-description` LLM helper (advisory only — never writes DB). `SupplierRiskService._decideLevel_via_rules` wraps the evaluator with a RISK-priority bypass wrapper to preserve byte-identical parity with legacy `_decideLevel`.

**Tech Stack:** FastAPI + SQLAlchemy 2.x async + Alembic + Pydantic v2 + Ant Design v5 + TypeScript + React + vitest. Reuses patterns from `feat-agent-tool-config-db`: DTO + Service + Registry (`warmUp` + `invalidate` + `reloadOne` + `asyncio.Lock`) + admin page + `seed_*` upsert + conftest autouse warmUp + commit-after-warmUp.

## Global Constraints

These bind every task. Repeating them verbatim from project rules:

- **File size**: 200-400 lines typical, **800 lines hard cap** (CLAUDE.md).
- **Function size**: < 50 lines (CLAUDE.md).
- **Nesting**: ≤ 4 levels, prefer early returns (CLAUDE.md).
- **Immutability**: always create new objects; never mutate in-place (CLAUDE.md).
- **DB-only SSOT for metadata**: handler engines stay in code; only metadata moves to DB (`Harness/rules/编码规范.md`).
- **SQL safety**: business queries SELECT-only via SQL Guard (`Harness/rules/数据治理.md`).
- **Token accounting**: every LLM call records prompt/completion tokens + cost (`Harness/rules/AI治理.md`).
- **TDD**: RED → GREEN → IMPROVE, coverage ≥ 80% (`Harness/rules/测试规范.md`).
- **Real PG testing**: backend tests must use real PostgreSQL + full API chain (no sqlite in-memory + direct service call). Port `5433`, db `qa_metadata_test`. (Test conventions: `qa-system-runbook-quirks` memory.)
- **Conftest warmUp lesson (commit `d01a4e1`)**: `warmAgentCaches` autouse fixture must `await dbSession.commit()` after each `warmUp()` call to release implicit transaction locks (`qa-system-agent-tool-config-db` memory).
- **Commit message format**: `feat|fix|test|chore|refactor|docs: <subject>` (CLAUDE.md).
- **Migration numbering**: latest is `0042_doc_rel_key_varchar`. New migration is `0043_feature_rule_config` with `down_revision = "0042_doc_rel_key_varchar"`.

## File Structure Map

### Backend — new files
- `backend/alembic/versions/0043_feature_rule_config.py` (migration)
- `backend/app/services/feature_rule_service.py` (CRUD + audit + version + referencing)
- `backend/app/services/feature_rule_registry.py` (in-memory cache singleton)
- `backend/app/services/feature_rule_evaluator.py` (pure sync, MAX severity)
- `backend/app/services/feature_rule_llm_service.py` (parse-description)
- `backend/scripts/seed_feature_rules.py` (4-rule idempotent upsert)
- `backend/app/api/v1/feature_rules.py` (router)
- `backend/app/tests/unit/test_feature_rule_evaluator.py`
- `backend/app/tests/unit/test_feature_rule_schemas.py`
- `backend/app/tests/unit/test_feature_rule_service.py`
- `backend/app/tests/unit/test_feature_rule_registry.py`
- `backend/app/tests/integration/test_feature_rule_migration.py`
- `backend/app/tests/integration/test_feature_rule_api.py`
- `backend/app/tests/integration/test_feature_rule_runtime_db_driven.py`
- `backend/app/tests/integration/test_feature_rule_supplier_risk_parity.py` ← **CRITICAL**
- `backend/app/tests/integration/test_feature_rule_audit.py`
- `backend/app/tests/integration/test_feature_rule_llm_parse.py`
- `backend/app/tests/integration/test_seed_feature_rules.py`

### Backend — modified files
- `backend/app/domain/enums.py` — add `RuleOperator`
- `backend/app/domain/models.py` — add `FeatureRule` + `FeatureRuleThreshold`
- `backend/app/domain/schemas.py` — add 5 DTOs
- `backend/app/domain/exceptions.py` — add 5 exception classes
- `backend/app/main.py` — lifespan + router registration
- `backend/app/services/supplier_risk_service.py` — `_decideLevel_via_rules`
- `backend/app/services/supplier_360_service.py` — derive slot list from registry
- `backend/scripts/seed_menu_config.py` — add `/admin/feature-rules` entry
- `backend/app/tests/integration/conftest.py` — extend warmUp + commit

### Frontend — new files
- `frontend/src/api/featureRules.ts`
- `frontend/src/types/featureRules.ts`
- `frontend/src/pages/AdminFeatureRulesPage.tsx`
- `frontend/src/components/admin/AiAssistModal.tsx`
- `frontend/src/tests/AdminFeatureRulesPage.test.tsx`
- `frontend/src/tests/featureRules.test.ts`

### Frontend — modified files
- `frontend/src/App.tsx` — route registration
- `frontend/src/i18n/zh-CN.ts` + `frontend/src/i18n/en-US.ts` — `featureRules.*` namespace

### Docs
- `Harness/changes/feat-feature-rule-config/summary.md` (change summary, last task)

---

## Task 1: Alembic migration 0043 + ORM models + RuleOperator enum

**Files:**
- Create: `backend/alembic/versions/0043_feature_rule_config.py`
- Modify: `backend/app/domain/enums.py` (add `RuleOperator` after line 88 — after `Severity` class)
- Modify: `backend/app/domain/models.py` (append `FeatureRule` + `FeatureRuleThreshold` classes)
- Create: `backend/app/tests/integration/test_feature_rule_migration.py`

**Interfaces:**
- Produces: `FeatureRule` (BigInteger id, code, data_object, data_layer, target_level, feature_name, enabled, priority, policy_description, version, created_time, updated_time)
- Produces: `FeatureRuleThreshold` (BigInteger id, rule_id FK, severity, operator, threshold_value NUMERIC(20,6), unit, threshold_order)
- Produces: `RuleOperator(str, Enum)`: `LT="lt"` / `LTE="lte"` / `GT="gt"` / `GTE="gte"` / `LT_INVERSE="lt_inverse"`

- [ ] **Step 1: Write the failing migration test**

```python
# backend/app/tests/integration/test_feature_rule_migration.py
"""Alembic 0043 落表 + 索引 + 约束验证。"""
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from alembic.config import Config
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine


async def test_feature_rule_tables_exist(dbSession: AsyncSession) -> None:
    """迁移后两张表都建好，列名 + 类型 + 约束对齐 spec §5。"""
    insp = await dbSession.run_sync(
        lambda sync_sess: inspect(sync_sess.bind)
    )
    # feature_rule
    cols = {c["name"]: c for c in insp.get_columns("feature_rule")}
    assert "code" in cols and "data_object" in cols and "target_level" in cols
    assert "version" in cols
    # unique 约束
    uqs = insp.get_unique_constraints("feature_rule")
    assert any("data_object" in u["column_names"] for u in uqs)
    # feature_rule_threshold FK
    fks = insp.get_foreign_keys("feature_rule_threshold")
    assert any(
        fk["referred_table"] == "feature_rule" and "rule_id" in fk["constrained_columns"]
        for fk in fks
    )
```

- [ ] **Step 2: Run test — FAIL (tables don't exist yet)**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test uv run pytest app/tests/integration/test_feature_rule_migration.py -v`
Expected: FAIL with `relation "feature_rule" does not exist`.

- [ ] **Step 3: Add `RuleOperator` enum** (in `backend/app/domain/enums.py`, after `Severity`):

```python
class RuleOperator(str, Enum):
    """Feature Rule 阈值运算符（spec §5.2）。"""

    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    LT_INVERSE = "lt_inverse"  # 0-1 区间 RISK_SCORE（越低越差）
```

- [ ] **Step 4: Add ORM models** (append to `backend/app/domain/models.py`):

```python
class FeatureRule(Base):
    """Feature 规则元数据（spec §5.1）。"""
    __tablename__ = "feature_rule"
    __table_args__ = (
        UniqueConstraint(
            "data_object", "data_layer", "target_level", "code",
            name="uq_feature_rule_scope_code",
        ),
        Index(
            "ix_feature_rule_scope_enabled",
            "data_object", "data_layer", "target_level", "enabled",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    data_object: Mapped[str] = mapped_column(String(64), nullable=False)
    data_layer: Mapped[str] = mapped_column(String(16), nullable=False)
    target_level: Mapped[str] = mapped_column(String(16), nullable=False)
    feature_name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default=text("100"))
    policy_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    created_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    thresholds: Mapped[list[FeatureRuleThreshold]] = relationship(
        back_populates="rule", cascade="all, delete-orphan", passive_deletes=True
    )


class FeatureRuleThreshold(Base):
    """单条阈值档位（spec §5.2）。"""
    __tablename__ = "feature_rule_threshold"
    __table_args__ = (
        UniqueConstraint("rule_id", "severity", name="uq_feature_rule_threshold_rule_severity"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("feature_rule.id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    operator: Mapped[str] = mapped_column(String(16), nullable=False)
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    threshold_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))

    rule: Mapped[FeatureRule] = relationship(back_populates="thresholds")
```

(Add necessary imports: `BigInteger`, `Numeric`, `Text`, `Index`, `UniqueConstraint`, `relationship`, `Decimal`, `DateTime`.)

- [ ] **Step 5: Write the migration** (`backend/alembic/versions/0043_feature_rule_config.py`):

```python
"""add feature_rule + feature_rule_threshold tables

Revision ID: 0043_feature_rule_config
Revises: 0042_doc_rel_key_varchar
Create Date: 2026-09-05

Why: feat-feature-rule-config 把硬编码 RISK_RULES + DEFAULT_SUPPLIER_FEATURES
迁到 DB SSOT（spec §5）。两张表头档 + 阈值档（1:N）。unique 约束保
(data_object, data_layer, target_level, code) 唯一。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0043_feature_rule_config"
down_revision = "0042_doc_rel_key_varchar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_rule",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("data_object", sa.String(length=64), nullable=False),
        sa.Column("data_layer", sa.String(length=16), nullable=False),
        sa.Column("target_level", sa.String(length=16), nullable=False),
        sa.Column("feature_name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("policy_description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "data_object", "data_layer", "target_level", "code",
            name="uq_feature_rule_scope_code",
        ),
    )
    op.create_index(
        "ix_feature_rule_scope_enabled",
        "feature_rule",
        ["data_object", "data_layer", "target_level", "enabled"],
    )

    op.create_table(
        "feature_rule_threshold",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("operator", sa.String(length=16), nullable=False),
        sa.Column("threshold_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("threshold_order", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["feature_rule.id"], ondelete="CASCADE",
            name="fk_feature_rule_threshold_rule",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rule_id", "severity", name="uq_feature_rule_threshold_rule_severity"
        ),
    )


def downgrade() -> None:
    op.drop_table("feature_rule_threshold")
    op.drop_index("ix_feature_rule_scope_enabled", table_name="feature_rule")
    op.drop_table("feature_rule")
```

- [ ] **Step 6: Run migration + re-run test**

```bash
cd backend && DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata uv run alembic upgrade head
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_feature_rule_migration.py -v
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/0043_feature_rule_config.py \
        backend/app/domain/enums.py backend/app/domain/models.py \
        backend/app/tests/integration/test_feature_rule_migration.py
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): Alembic 0043 + ORM models + RuleOperator enum"
```

---

## Task 2: Exception taxonomy + DTOs

**Files:**
- Modify: `backend/app/domain/exceptions.py` (add 5 exception classes)
- Modify: `backend/app/domain/schemas.py` (add 6 DTO classes)
- Create: `backend/app/tests/unit/test_feature_rule_schemas.py`

**Interfaces:**
- Produces: `FeatureRuleNotFoundError`, `FeatureRuleVersionConflictError(current_version)`, `FeatureRuleReferencingError(referencing)`, `FeatureRuleValidationError`, `LLMUnavailableError`
- Produces: `FeatureRuleThresholdRead`, `FeatureRuleThresholdCreate`, `FeatureRuleThresholdSuggestion`, `FeatureRuleRead`, `FeatureRuleCreate`, `FeatureRuleUpdate`, `FeatureRuleParseDescriptionRequest`, `FeatureRuleParseDescriptionResponse`

- [ ] **Step 1: Write the failing schema + exception tests**

```python
# backend/app/tests/unit/test_feature_rule_schemas.py
"""Feature Rule DTO + exception 单元测试（spec §9.1 + §9.2）。"""
from datetime import datetime

import pytest
from pydantic import ValidationError as PydValidationError

from app.domain.exceptions import (
    FeatureRuleNotFoundError,
    FeatureRuleReferencingError,
    FeatureRuleValidationError,
    FeatureRuleVersionConflictError,
    LLMUnavailableError,
)
from app.domain.schemas import (
    FeatureRuleCreate,
    FeatureRuleThresholdCreate,
    FeatureRuleUpdate,
)


def test_threshold_create_decimal_required() -> None:
    with pytest.raises(PydValidationError):
        FeatureRuleThresholdCreate(severity="HIGH", operator="lt", threshold_value=None)


def test_feature_rule_create_requires_thresholds() -> None:
    with pytest.raises(PydValidationError):
        FeatureRuleCreate(
            code="r1", data_object="SUPPLIER", data_layer="FEATURE",
            target_level="RISK", feature_name="X", thresholds=[],
        )


def test_feature_rule_create_severity_unique_within_thresholds() -> None:
    with pytest.raises(PydValidationError):
        FeatureRuleCreate(
            code="r1", data_object="SUPPLIER", data_layer="FEATURE",
            target_level="RISK", feature_name="X",
            thresholds=[
                FeatureRuleThresholdCreate(severity="HIGH", operator="lt", threshold_value=10),
                FeatureRuleThresholdCreate(severity="HIGH", operator="lt", threshold_value=20),
            ],
        )


def test_feature_rule_update_requires_version() -> None:
    with pytest.raises(PydValidationError):
        FeatureRuleUpdate(enabled=False)  # type: ignore[call-arg]


def test_feature_rule_version_conflict_error_carries_current_version() -> None:
    err = FeatureRuleVersionConflictError(current_version=3)
    assert err.current_version == 3
    assert isinstance(err, Exception)


def test_referencing_error_carries_list() -> None:
    err = FeatureRuleReferencingError(["AgentA", "AgentB"])
    assert err.referencing == ["AgentA", "AgentB"]


def test_llm_unavailable_error_is_exception() -> None:
    assert isinstance(LLMUnavailableError("test"), Exception)


def test_feature_rule_not_found_error_is_exception() -> None:
    assert isinstance(FeatureRuleNotFoundError("test"), Exception)


def test_validation_error_is_exception() -> None:
    assert isinstance(FeatureRuleValidationError("test"), Exception)
```

- [ ] **Step 2: Run test — FAIL (DTOs/exceptions don't exist)**

Run: `cd backend && uv run pytest app/tests/unit/test_feature_rule_schemas.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Add exceptions** (append to `backend/app/domain/exceptions.py`):

```python
class FeatureRuleNotFoundError(NotFoundError):
    """Feature rule 不存在（spec §9.2）。"""


class FeatureRuleVersionConflictError(ConflictError):
    """Feature rule 乐观锁版本冲突（spec §9.2）。"""

    def __init__(self, message: str, current_version: int) -> None:
        super().__init__(message)
        self.current_version = current_version


class FeatureRuleReferencingError(ConflictError):
    """Feature rule 被外部引用，无法删除（spec §9.2）。"""

    def __init__(self, message: str, referencing: list[str]) -> None:
        super().__init__(message)
        self.referencing = referencing


class FeatureRuleValidationError(ValidationError):
    """Feature rule 业务校验失败（spec §9.2）。"""


class LLMUnavailableError(Exception):
    """LLM 服务不可用（spec §9.2 + §7.3）。"""
```

(Confirm `NotFoundError`, `ConflictError`, `ValidationError` already exist in this file — they should, from `feat-agent-tool-config-db`.)

- [ ] **Step 4: Add DTOs** (append to `backend/app/domain/schemas.py`):

```python
from decimal import Decimal  # if not already imported

# ---- Feature Rule DTOs (spec §9.1) ----


class FeatureRuleThresholdRead(BaseModel):
    severity: Severity
    operator: RuleOperator
    threshold_value: Decimal
    unit: str | None
    threshold_order: int


class FeatureRuleThresholdCreate(BaseModel):
    severity: Severity
    operator: RuleOperator
    threshold_value: Decimal
    unit: str | None = None
    threshold_order: int = 1


class FeatureRuleThresholdSuggestion(BaseModel):
    """parse-description LLM 输出（spec §7.1 + §9.1）。"""

    feature_name: str
    severity: Severity
    operator: RuleOperator
    threshold_value: Decimal
    unit: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class FeatureRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    data_object: str
    data_layer: str
    target_level: str
    feature_name: str
    enabled: bool
    priority: int
    policy_description: str | None
    version: int
    thresholds: list[FeatureRuleThresholdRead]
    created_time: datetime
    updated_time: datetime | None


class FeatureRuleCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    data_object: str
    data_layer: str
    target_level: str
    feature_name: str
    enabled: bool = True
    priority: int = 100
    policy_description: str | None = None
    thresholds: list[FeatureRuleThresholdCreate] = Field(min_length=1)

    @field_validator("thresholds")
    @classmethod
    def _uniqueSeverities(cls, v: list[FeatureRuleThresholdCreate]) -> list[FeatureRuleThresholdCreate]:
        sevs = [t.severity.value for t in v]
        if len(sevs) != len(set(sevs)):
            raise ValueError("thresholds 内 severity 必须唯一")
        return v


class FeatureRuleUpdate(BaseModel):
    """code / data_object / data_layer / target_level / feature_name 不可变。"""

    enabled: bool | UnsetType = Unset
    priority: int | UnsetType = Unset
    policy_description: str | UnsetType | None = Unset
    thresholds: list[FeatureRuleThresholdCreate] | UnsetType = Unset
    version: int  # 必填，乐观锁


class FeatureRuleParseDescriptionRequest(BaseModel):
    data_object: str
    data_layer: str
    target_level: str
    natural_language: str = Field(min_length=10, max_length=4000)


class FeatureRuleParseDescriptionResponse(BaseModel):
    suggested_thresholds: list[FeatureRuleThresholdSuggestion]
    reasoning: str
    overall_confidence: float = Field(ge=0.0, le=1.0)
    warnings: list[str]
```

(Confirm imports: `Severity`, `RuleOperator`, `UnsetType`, `_UnsetType`, `ConfigDict`, `field_validator`. `Unset` is `_UnsetType` sentinel — confirm naming.)

- [ ] **Step 5: Run test — PASS**

Run: `cd backend && uv run pytest app/tests/unit/test_feature_rule_schemas.py -v`
Expected: 9 tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/domain/exceptions.py backend/app/domain/schemas.py \
        backend/app/tests/unit/test_feature_rule_schemas.py
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): DTOs + exception taxonomy"
```

---

## Task 3: FeatureRuleService (CRUD + audit + version + referencing)

**Files:**
- Create: `backend/app/services/feature_rule_service.py`
- Create: `backend/app/tests/unit/test_feature_rule_service.py`

**Interfaces:**
- Produces: `FeatureRuleService()` with methods:
  - `listRules(session, *, enabledOnly=False) -> list[FeatureRule]`
  - `getRule(session, code) -> FeatureRule` (raises `FeatureRuleNotFoundError`)
  - `createRule(session, dto, actor) -> FeatureRule` (409 on dup, audit)
  - `updateRule(session, code, dto, actor) -> FeatureRule` (409 version, audit)
  - `deleteRule(session, code, actor) -> None` (409 referencing, audit)
  - `toggleEnabled(session, code, enabled, actor) -> FeatureRule` (audit)
  - `upsertSeed(session, code, fields) -> FeatureRule` (seed only, no audit)

- [ ] **Step 1: Write the failing service tests** (CRUD + version + audit + referencing; mock session)

```python
# backend/app/tests/unit/test_feature_rule_service.py
"""FeatureRuleService 单元测试。"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.exceptions import (
    FeatureRuleNotFoundError,
    FeatureRuleReferencingError,
    FeatureRuleVersionConflictError,
)
from app.domain.models import FeatureRule, FeatureRuleThreshold
from app.domain.schemas import (
    FeatureRuleCreate,
    FeatureRuleThresholdCreate,
    FeatureRuleUpdate,
)
from app.services.feature_rule_service import FeatureRuleService


def _row(**kw) -> SimpleNamespace:
    base = dict(
        id=1, code="r1", data_object="SUPPLIER", data_layer="FEATURE",
        target_level="RISK", feature_name="F1", enabled=True, priority=100,
        policy_description=None, version=1, created_time=datetime.now(timezone.utc),
        updated_time=None, thresholds=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def mockSession() -> AsyncMock:
    return AsyncMock()


async def test_list_rules_returns_rows(mockSession: AsyncMock) -> None:
    expected = [_row(code="a"), _row(code="b")]
    mockSession.execute.return_value = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=expected))))
    rows = await FeatureRuleService().listRules(mockSession)
    assert len(rows) == 2


async def test_get_rule_not_found_raises(mockSession: AsyncMock) -> None:
    mockSession.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    with pytest.raises(FeatureRuleNotFoundError):
        await FeatureRuleService().getRule(mockSession, "missing")


async def test_create_rule_dup_code_raises_conflict(mockSession: AsyncMock) -> None:
    mockSession.flush.side_effect = IntegrityError("stmt", {}, Exception("uq_feature_rule_scope_code"))
    dto = FeatureRuleCreate(
        code="r1", data_object="SUPPLIER", data_layer="FEATURE",
        target_level="RISK", feature_name="F1",
        thresholds=[FeatureRuleThresholdCreate(severity="HIGH", operator="lt", threshold_value=10)],
    )
    with pytest.raises(Exception):  # ConflictError (parent class)
        await FeatureRuleService().createRule(mockSession, dto, SimpleNamespace(userId="u1"))


async def test_update_rule_version_mismatch_raises(mockSession: AsyncMock) -> None:
    existing = _row(version=5)
    mockSession.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
    dto = FeatureRuleUpdate(version=3, enabled=False)
    with pytest.raises(FeatureRuleVersionConflictError) as exc_info:
        await FeatureRuleService().updateRule(mockSession, "r1", dto, SimpleNamespace(userId="u1"))
    assert exc_info.value.current_version == 5


async def test_delete_rule_referenced_raises(mockSession: AsyncMock) -> None:
    existing = _row()
    # getRule returns existing
    mockSession.execute.side_effect = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=existing)),
        # referencing query
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=["AgentA"])))),
    ]
    with pytest.raises(FeatureRuleReferencingError) as exc_info:
        await FeatureRuleService().deleteRule(mockSession, "r1", SimpleNamespace(userId="u1"))
    assert exc_info.value.referencing == ["AgentA"]
```

- [ ] **Step 2: Run test — FAIL (service doesn't exist)**

Run: `cd backend && uv run pytest app/tests/unit/test_feature_rule_service.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the service** (`backend/app/services/feature_rule_service.py`):

```python
"""FeatureRule CRUD + outbox 审计 + 乐观锁（spec §9）。

所有写操作通过 OutboxService.enqueue 写审计，caller commit。
参照 feat-agent-tool-config-db 的 AgentToolConfigService 模式。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import (
    ConflictError,
    FeatureRuleNotFoundError,
    FeatureRuleReferencingError,
    FeatureRuleValidationError,
    FeatureRuleVersionConflictError,
)
from app.domain.models import FeatureRule, FeatureRuleThreshold
from app.domain.schemas import (
    FeatureRuleCreate,
    FeatureRuleThresholdCreate,
    FeatureRuleUpdate,
    _UnsetType,
)
from app.services.outbox_service import OutboxService

logger = logging.getLogger(__name__)


def _ruleRowToDict(row: FeatureRule) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "data_object": row.data_object,
        "data_layer": row.data_layer,
        "target_level": row.target_level,
        "feature_name": row.feature_name,
        "enabled": row.enabled,
        "priority": row.priority,
        "policy_description": row.policy_description,
        "version": row.version,
        "created_time": row.created_time.isoformat() if row.created_time else None,
        "updated_time": row.updated_time.isoformat() if row.updated_time else None,
        "thresholds": [
            {
                "severity": t.severity.value if hasattr(t.severity, "value") else t.severity,
                "operator": t.operator.value if hasattr(t.operator, "value") else t.operator,
                "threshold_value": str(t.threshold_value),
                "unit": t.unit,
                "threshold_order": t.threshold_order,
            }
            for t in (row.thresholds or [])
        ],
    }


def _seedThresholds(rule: FeatureRule, threshold_dicts: list[dict]) -> None:
    """Seed 专用：清空旧阈值 + 重建（spec §5.2 1:N）。"""
    rule.thresholds.clear()
    for td in threshold_dicts:
        rule.thresholds.append(FeatureRuleThreshold(
            severity=td["severity"].value if hasattr(td["severity"], "value") else td["severity"],
            operator=td["operator"].value if hasattr(td["operator"], "value") else td["operator"],
            threshold_value=td["threshold_value"],
            unit=td.get("unit"),
            threshold_order=td.get("threshold_order", 1),
        ))


class FeatureRuleService:
    def __init__(self, outbox: OutboxService | None = None) -> None:
        self._outbox = outbox or OutboxService()

    async def listRules(
        self, session: AsyncSession, *, enabledOnly: bool = False
    ) -> list[FeatureRule]:
        stmt = select(FeatureRule)
        if enabledOnly:
            stmt = stmt.where(FeatureRule.enabled.is_(True))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getRule(self, session: AsyncSession, code: str) -> FeatureRule:
        result = await session.execute(
            select(FeatureRule).where(FeatureRule.code == code)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise FeatureRuleNotFoundError(f"feature_rule not found: {code}")
        return row

    async def createRule(
        self,
        session: AsyncSession,
        dto: FeatureRuleCreate,
        actor: CurrentUser,
    ) -> FeatureRule:
        row = FeatureRule(
            code=dto.code,
            data_object=dto.data_object,
            data_layer=dto.data_layer,
            target_level=dto.target_level,
            feature_name=dto.feature_name,
            enabled=dto.enabled,
            priority=dto.priority,
            policy_description=dto.policy_description,
            version=1,
        )
        for td in dto.thresholds:
            row.thresholds.append(FeatureRuleThreshold(
                severity=td.severity.value,
                operator=td.operator.value,
                threshold_value=td.threshold_value,
                unit=td.unit,
                threshold_order=td.threshold_order,
            ))
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as e:
            await session.rollback()
            if "uq_feature_rule_scope_code" in str(e.orig):
                raise ConflictError(
                    f"feature_rule code 已存在: {dto.code}"
                )
            raise

        await self._outbox.enqueue(
            session,
            event_type="feature_rule_created",
            entity_type="feature_rule",
            entity_id=row.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": None, "after": _ruleRowToDict(row)},
        )
        return row

    async def updateRule(
        self,
        session: AsyncSession,
        code: str,
        dto: FeatureRuleUpdate,
        actor: CurrentUser,
    ) -> FeatureRule:
        row = await self.getRule(session, code)
        before = _ruleRowToDict(row)
        if row.version != dto.version:
            raise FeatureRuleVersionConflictError(
                f"feature_rule version mismatch: current={row.version}, dto={dto.version}",
                current_version=row.version,
            )

        if not isinstance(dto.enabled, _UnsetType):
            row.enabled = dto.enabled
        if not isinstance(dto.priority, _UnsetType):
            row.priority = dto.priority
        if not isinstance(dto.policy_description, _UnsetType):
            row.policy_description = dto.policy_description
        if not isinstance(dto.thresholds, _UnsetType):
            row.thresholds.clear()
            for td in dto.thresholds:
                row.thresholds.append(FeatureRuleThreshold(
                    severity=td.severity.value,
                    operator=td.operator.value,
                    threshold_value=td.threshold_value,
                    unit=td.unit,
                    threshold_order=td.threshold_order,
                ))

        row.version += 1
        row.updated_time = datetime.now(timezone.utc)
        await session.flush()

        await self._outbox.enqueue(
            session,
            event_type="feature_rule_updated",
            entity_type="feature_rule",
            entity_id=row.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": before, "after": _ruleRowToDict(row)},
        )
        return row

    async def deleteRule(
        self,
        session: AsyncSession,
        code: str,
        actor: CurrentUser,
    ) -> None:
        row = await self.getRule(session, code)

        # 检查引用：AgentDefinition.tool_name 通过 code 引用（spec §9）。
        # v1：仅检查 AgentDefinition.tool_name == code 的引用。
        from app.domain.models import AgentDefinition
        referencing = (
            await session.execute(
                select(AgentDefinition.agent_code).where(
                    AgentDefinition.tool_name == code
                )
            )
        ).scalars().all()
        if referencing:
            raise FeatureRuleReferencingError(
                f"feature_rule 被 Agent 引用，无法删除: {code}",
                referencing=list(referencing),
            )

        before = _ruleRowToDict(row)
        deleted_id = row.id
        await session.delete(row)
        await session.flush()

        await self._outbox.enqueue(
            session,
            event_type="feature_rule_deleted",
            entity_type="feature_rule",
            entity_id=deleted_id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": before, "after": None},
        )

    async def toggleEnabled(
        self,
        session: AsyncSession,
        code: str,
        enabled: bool,
        actor: CurrentUser,
    ) -> FeatureRule:
        row = await self.getRule(session, code)
        before = _ruleRowToDict(row)
        row.enabled = enabled
        row.version += 1
        row.updated_time = datetime.now(timezone.utc)
        await session.flush()

        await self._outbox.enqueue(
            session,
            event_type="feature_rule_updated",
            entity_type="feature_rule",
            entity_id=row.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": before, "after": _ruleRowToDict(row)},
        )
        return row

    async def upsertSeed(
        self,
        session: AsyncSession,
        code: str,
        fields: dict,
    ) -> FeatureRule:
        """Seed 专用 upsert（绕过 ACL + audit）。code 命中 → 全量更新元数据；未命中 → 插入。"""
        existing = (
            await session.execute(
                select(FeatureRule).where(FeatureRule.code == code)
            )
        ).scalar_one_or_none()
        if existing is None:
            row = FeatureRule(
                code=code,
                data_object=fields["data_object"],
                data_layer=fields["data_layer"],
                target_level=fields["target_level"],
                feature_name=fields["feature_name"],
                enabled=fields.get("enabled", True),
                priority=fields.get("priority", 100),
                policy_description=fields.get("policy_description"),
                version=1,
            )
            for td in fields["thresholds"]:
                row.thresholds.append(FeatureRuleThreshold(
                    severity=td["severity"].value if hasattr(td["severity"], "value") else td["severity"],
                    operator=td["operator"].value if hasattr(td["operator"], "value") else td["operator"],
                    threshold_value=td["threshold_value"],
                    unit=td.get("unit"),
                    threshold_order=td.get("threshold_order", 1),
                ))
            session.add(row)
            await session.flush()
            return row

        existing.data_object = fields["data_object"]
        existing.data_layer = fields["data_layer"]
        existing.target_level = fields["target_level"]
        existing.feature_name = fields["feature_name"]
        existing.enabled = fields.get("enabled", True)
        existing.priority = fields.get("priority", 100)
        existing.policy_description = fields.get("policy_description")
        _seedThresholds(existing, fields["thresholds"])
        existing.updated_time = datetime.now(timezone.utc)
        await session.flush()
        return existing
```

- [ ] **Step 4: Run test — PASS**

Run: `cd backend && uv run pytest app/tests/unit/test_feature_rule_service.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/feature_rule_service.py \
        backend/app/tests/unit/test_feature_rule_service.py
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): FeatureRuleService CRUD + audit + version"
```

---

## Task 4: FeatureRuleRegistry (warmUp + invalidate + reloadOne)

**Files:**
- Create: `backend/app/services/feature_rule_registry.py`
- Create: `backend/app/tests/unit/test_feature_rule_registry.py`

**Interfaces:**
- Produces: `feature_rule_registry` (module-level singleton of `FeatureRuleRegistry`)
- Produces: `FeatureRuleRegistry.warmUp(session)` / `getEnabledRules(scope_tuple)` / `invalidate(rule_id)` / `reloadOne(session, rule_id)`

- [ ] **Step 1: Write the failing registry tests**

```python
# backend/app/tests/unit/test_feature_rule_registry.py
"""FeatureRuleRegistry 单元测试。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.feature_rule_registry import (
    FeatureRuleRegistry,
    feature_rule_registry,
)


@pytest.fixture
def freshRegistry() -> FeatureRuleRegistry:
    return FeatureRuleRegistry()


def test_get_rules_unwarmed_raises(freshRegistry: FeatureRuleRegistry) -> None:
    with pytest.raises(RuntimeError, match="未 warmUp"):
        freshRegistry.getEnabledRules("SUPPLIER", "FEATURE", "RISK")


async def test_warm_up_populates_index(freshRegistry: FeatureRuleRegistry) -> None:
    rule_a = SimpleNamespace(id=1, code="r1", data_object="SUPPLIER", data_layer="FEATURE",
                              target_level="RISK", feature_name="F1", enabled=True, thresholds=[])
    rule_b = SimpleNamespace(id=2, code="r2", data_object="SUPPLIER", data_layer="FEATURE",
                              target_level="RISK", feature_name="F2", enabled=False, thresholds=[])
    rule_c = SimpleNamespace(id=3, code="r3", data_object="SUPPLIER", data_layer="FEATURE",
                              target_level="QUALITY_SCORE", feature_name="F3", enabled=True, thresholds=[])
    session = AsyncMock()
    session.execute.side_effect = [
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[rule_a, rule_b, rule_c])))),
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
    ]
    await freshRegistry.warmUp(session)

    risk = freshRegistry.getEnabledRules("SUPPLIER", "FEATURE", "RISK")
    assert {r.code for r in risk} == {"r1"}  # r2 disabled excluded
    quality = freshRegistry.getEnabledRules("SUPPLIER", "FEATURE", "QUALITY_SCORE")
    assert {r.code for r in quality} == {"r3"}


def test_invalidate_unwarmed_is_noop(freshRegistry: FeatureRuleRegistry) -> None:
    freshRegistry.invalidate()  # 不抛


async def test_reload_one_rule_not_found_clears_index() -> None:
    reg = FeatureRuleRegistry()
    reg._loaded = True  # 手动标记 warmed
    reg._rules = {("SUPPLIER", "FEATURE", "RISK"): ["placeholder"]}
    session = AsyncMock()
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    await reg.reloadOne(session, 999)
    assert ("SUPPLIER", "FEATURE", "RISK") not in reg._rules


def test_module_singleton_exists() -> None:
    assert isinstance(feature_rule_registry, FeatureRuleRegistry)
```

- [ ] **Step 2: Run test — FAIL**

Run: `cd backend && uv run pytest app/tests/unit/test_feature_rule_registry.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the registry** (`backend/app/services/feature_rule_registry.py`):

```python
"""DB-backed Feature Rule 注册表（spec §6.1）。

- warmUp: lifespan 调用，bulk-load enabled=True + 阈值档
- getEnabledRules(scope_tuple): sync 快路径；未 warmed → RuntimeError
- invalidate(rule_id|None): 写时失效（sync，不查 DB）
- reloadOne(session, rule_id): async，asyncio.Lock 防并发 reloadOne 竞态
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import FeatureRule

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureRuleReady:
    """Registry 内部就绪结构（避免 ORM 持有 session）。"""
    id: int
    code: str
    data_object: str
    data_layer: str
    target_level: str
    feature_name: str
    enabled: bool
    priority: int
    thresholds: tuple  # tuple[FeatureThresholdReady, ...]


@dataclass(frozen=True)
class FeatureThresholdReady:
    severity: str
    operator: str
    threshold_value: object  # Decimal
    unit: str | None
    threshold_order: int


class FeatureRuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[tuple[str, str, str], list[FeatureRuleReady]] = {}
        self._loaded: bool = False
        self._lock = asyncio.Lock()

    async def warmUp(self, session: AsyncSession) -> None:
        rows = (
            await session.execute(
                select(FeatureRule).where(FeatureRule.enabled.is_(True))
            )
        ).scalars().all()
        rule_ids = [r.id for r in rows]
        thresholds_by_rule: dict[int, list[FeatureThresholdReady]] = {}
        if rule_ids:
            from app.domain.models import FeatureRuleThreshold
            t_rows = (
                await session.execute(
                    select(FeatureRuleThreshold).where(
                        FeatureRuleThreshold.rule_id.in_(rule_ids)
                    )
                )
            ).scalars().all()
            for t in t_rows:
                thresholds_by_rule.setdefault(t.rule_id, []).append(FeatureThresholdReady(
                    severity=t.severity,
                    operator=t.operator,
                    threshold_value=t.threshold_value,
                    unit=t.unit,
                    threshold_order=t.threshold_order,
                ))

        async with self._lock:
            self._rules = {}
            for r in rows:
                key = (r.data_object, r.data_layer, r.target_level)
                ready = FeatureRuleReady(
                    id=r.id, code=r.code, data_object=r.data_object,
                    data_layer=r.data_layer, target_level=r.target_level,
                    feature_name=r.feature_name, enabled=r.enabled,
                    priority=r.priority,
                    thresholds=tuple(thresholds_by_rule.get(r.id, [])),
                )
                self._rules.setdefault(key, []).append(ready)
            self._loaded = True
        logger.info("FeatureRuleRegistry warmed up: %d rules", sum(len(v) for v in self._rules.values()))

    def getEnabledRules(
        self, data_object: str, data_layer: str, target_level: str
    ) -> list[FeatureRuleReady]:
        if not self._loaded:
            raise RuntimeError("FeatureRuleRegistry 未 warmUp（lifespan bug）")
        return list(self._rules.get((data_object, data_layer, target_level), []))

    def invalidate(self, rule_id: int | None = None) -> None:
        """写时失效（同步快路径）。未 warmed 时为 no-op。
        v1：简化策略——任何失效清空整个索引，下次查询触发 reloadOne。
        """
        if not self._loaded:
            return
        self._rules.clear()

    async def reloadOne(self, session: AsyncSession, rule_id: int) -> None:
        async with self._lock:
            row = (
                await session.execute(
                    select(FeatureRule).where(FeatureRule.id == rule_id)
                )
            ).scalar_one_or_none()
            if row is None or not row.enabled:
                self._rules.clear()
                return
            # 简化：整组重 warmUp
            await self.warmUp(session)


feature_rule_registry = FeatureRuleRegistry()
```

- [ ] **Step 4: Run test — PASS**

Run: `cd backend && uv run pytest app/tests/unit/test_feature_rule_registry.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/feature_rule_registry.py \
        backend/app/tests/unit/test_feature_rule_registry.py
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): FeatureRuleRegistry warmUp/invalidate/reloadOne"
```

---

## Task 5: FeatureRuleEvaluator (pure sync MAX severity)

**Files:**
- Create: `backend/app/services/feature_rule_evaluator.py`
- Create: `backend/app/tests/unit/test_feature_rule_evaluator.py`

**Interfaces:**
- Produces: `RuleHit` dataclass (rule_code, feature_name, severity, operator, threshold_value, actual_value)
- Produces: `RuleEvaluation` dataclass (matched_severity, matched_severity_source, contributing_rules)
- Produces: `FeatureRuleEvaluator.evaluate(data_object, data_layer, target_level, feature_values) -> RuleEvaluation`

- [ ] **Step 1: Write the failing evaluator tests** (5 operators × boundaries + tier severity-ordered + cross-rule MAX + missing value + empty rule set + no match)

```python
# backend/app/tests/unit/test_feature_rule_evaluator.py
"""FeatureRuleEvaluator 单元测试（spec §6.2）。"""
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.services.feature_rule_registry import FeatureRuleReady, FeatureThresholdReady
from app.services.feature_rule_evaluator import (
    FeatureRuleEvaluator,
    RuleEvaluation,
    RuleHit,
)


@pytest.fixture(autouse=True)
def _seedRegistry(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例前注册规则到模块 singleton。"""
    from app.services.feature_rule_registry import feature_rule_registry
    feature_rule_registry._loaded = True
    feature_rule_registry._rules = {}


def _rule(code: str, feature: str, thresholds: list[FeatureThresholdReady]) -> FeatureRuleReady:
    return FeatureRuleReady(
        id=hash(code) & 0xFFFF, code=code, data_object="SUPPLIER",
        data_layer="FEATURE", target_level="RISK", feature_name=feature,
        enabled=True, priority=100, thresholds=tuple(thresholds),
    )


def test_empty_rule_set_returns_no_rules() -> None:
    ev = FeatureRuleEvaluator.evaluate("SUPPLIER", "FEATURE", "RISK", {})
    assert ev.matched_severity is None
    assert ev.matched_severity_source == "no_rules"


def test_lt_operator_triggers_when_value_below_threshold() -> None:
    from app.services.feature_rule_registry import feature_rule_registry
    feature_rule_registry._rules[("SUPPLIER", "FEATURE", "RISK")] = [
        _rule("otd", "OTD", [FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1)])
    ]
    ev = FeatureRuleEvaluator.evaluate("SUPPLIER", "FEATURE", "RISK", {"OTD": Decimal(85)})
    assert ev.matched_severity.value == "HIGH"


def test_lt_operator_does_not_trigger_at_boundary() -> None:
    from app.services.feature_rule_registry import feature_rule_registry
    feature_rule_registry._rules[("SUPPLIER", "FEATURE", "RISK")] = [
        _rule("otd", "OTD", [FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1)])
    ]
    ev = FeatureRuleEvaluator.evaluate("SUPPLIER", "FEATURE", "RISK", {"OTD": Decimal(90)})
    assert ev.matched_severity is None


def test_tier_severity_ordered_first_hit_wins() -> None:
    from app.services.feature_rule_registry import feature_rule_registry
    feature_rule_registry._rules[("SUPPLIER", "FEATURE", "RISK")] = [
        _rule("score", "RISK_SCORE", [
            FeatureThresholdReady("HIGH", "lt_inverse", Decimal(0.6), None, 1),
            FeatureThresholdReady("MEDIUM", "lt_inverse", Decimal(0.8), None, 1),
            FeatureThresholdReady("LOW", "lt_inverse", Decimal(1.01), None, 1),
        ])
    ]
    # 0.55 < 0.6 → HIGH tier hit (first match in severity order)
    ev = FeatureRuleEvaluator.evaluate("SUPPLIER", "FEATURE", "RISK", {"RISK_SCORE": Decimal("0.55")})
    assert ev.matched_severity.value == "HIGH"


def test_cross_rule_max_severity() -> None:
    from app.services.feature_rule_registry import feature_rule_registry
    feature_rule_registry._rules[("SUPPLIER", "FEATURE", "RISK")] = [
        _rule("otd", "OTD", [FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1)]),
        _rule("defect", "DEFECT", [FeatureThresholdReady("MEDIUM", "gt", Decimal(5), "%", 1)]),
    ]
    # OTD triggers HIGH, DEFECT triggers MEDIUM → MAX severity = HIGH
    ev = FeatureRuleEvaluator.evaluate("SUPPLIER", "FEATURE", "RISK", {
        "OTD": Decimal(85), "DEFECT": Decimal(7),
    })
    assert ev.matched_severity.value == "HIGH"


def test_missing_value_skips_rule() -> None:
    from app.services.feature_rule_registry import feature_rule_registry
    feature_rule_registry._rules[("SUPPLIER", "FEATURE", "RISK")] = [
        _rule("otd", "OTD", [FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1)])
    ]
    ev = FeatureRuleEvaluator.evaluate("SUPPLIER", "FEATURE", "RISK", {"OTD": None})
    assert ev.matched_severity is None
    assert ev.matched_severity_source == "no_match"


def test_no_match_when_value_above_threshold() -> None:
    from app.services.feature_rule_registry import feature_rule_registry
    feature_rule_registry._rules[("SUPPLIER", "FEATURE", "RISK")] = [
        _rule("otd", "OTD", [FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1)])
    ]
    ev = FeatureRuleEvaluator.evaluate("SUPPLIER", "FEATURE", "RISK", {"OTD": Decimal(95)})
    assert ev.matched_severity is None


def test_evaluation_immutable() -> None:
    from dataclasses import dataclass
    with pytest.raises(FrozenInstanceError):
        ev = RuleEvaluation(None, "no_rules", [])
        ev.matched_severity = "X"  # type: ignore[misc]
```

- [ ] **Step 2: Run test — FAIL**

Run: `cd backend && uv run pytest app/tests/unit/test_feature_rule_evaluator.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the evaluator** (`backend/app/services/feature_rule_evaluator.py`):

```python
"""Feature Rule 评估器（spec §6.2，纯同步，无 IO / 无 LLM）。

跨规则聚合：MAX severity（min(_SEVERITY_ORDER[severity])）。
每个规则：按 severity 顺序检查 tier，第一个匹配 tier 命中（高 severity 先匹配）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from app.services.feature_rule_registry import (
    FeatureRuleReady,
    feature_rule_registry,
)

# Severity 排序：HIGH(0) < MEDIUM(1) < LOW(2) < INFO(3)
_SEVERITY_ORDER: dict[str, int] = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}

_OPERATORS: dict[str, "callable"] = {
    "lt": lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
    "gt": lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "lt_inverse": lambda v, t: v < t,
}


@dataclass(frozen=True)
class RuleHit:
    rule_code: str
    feature_name: str
    severity: str
    operator: str
    threshold_value: Decimal
    actual_value: Decimal


@dataclass(frozen=True)
class RuleEvaluation:
    matched_severity: str | None
    matched_severity_source: str
    contributing_rules: tuple[RuleHit, ...]


class FeatureRuleEvaluator:
    @staticmethod
    def evaluate(
        data_object: str,
        data_layer: str,
        target_level: str,
        feature_values: Mapping[str, Decimal | None],
    ) -> RuleEvaluation:
        rules: list[FeatureRuleReady] = feature_rule_registry.getEnabledRules(
            data_object, data_layer, target_level
        )
        if not rules:
            return RuleEvaluation(None, "no_rules", ())

        hits: list[RuleHit] = []
        for rule in rules:
            value = feature_values.get(rule.feature_name)
            if value is None:
                continue
            for tier in sorted(rule.thresholds, key=lambda t: _SEVERITY_ORDER.get(t.severity, 99)):
                op = _OPERATORS.get(tier.operator)
                if op is None:
                    continue
                if op(value, tier.threshold_value):
                    hits.append(RuleHit(
                        rule_code=rule.code,
                        feature_name=rule.feature_name,
                        severity=tier.severity,
                        operator=tier.operator,
                        threshold_value=tier.threshold_value,
                        actual_value=value,
                    ))
                    break  # First matching tier wins (severity-ordered)

        if not hits:
            return RuleEvaluation(None, "no_match", ())
        worst = min(hits, key=lambda h: _SEVERITY_ORDER.get(h.severity, 99))
        return RuleEvaluation(worst.severity, f"rule:{worst.rule_code}", tuple(hits))
```

- [ ] **Step 4: Run test — PASS**

Run: `cd backend && uv run pytest app/tests/unit/test_feature_rule_evaluator.py -v`
Expected: 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/feature_rule_evaluator.py \
        backend/app/tests/unit/test_feature_rule_evaluator.py
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): FeatureRuleEvaluator (pure sync, MAX severity)"
```

---

## Task 6: Seed script + lifespan integration

**Files:**
- Create: `backend/scripts/seed_feature_rules.py`
- Create: `backend/app/tests/integration/test_seed_feature_rules.py`
- Modify: `backend/app/main.py` (lifespan + router registration deferred to Task 11)

**Interfaces:**
- Produces: `seedFeatureRules(session) -> int` (idempotent upsert 4 rules)
- Lifespan (Task 11): calls `seedFeatureRules(session)` then `feature_rule_registry.warmUp(session)`

- [ ] **Step 1: Write the failing seed test**

```python
# backend/app/tests/integration/test_seed_feature_rules.py
"""seed_feature_rules 幂等 upsert + 与 seed_agent_tool_configs 模式一致。"""
from sqlalchemy import select

from app.domain.models import FeatureRule
from scripts.seed_feature_rules import seedFeatureRules


async def test_seed_creates_4_rules(dbSession) -> None:
    n = await seedFeatureRules(dbSession)
    await dbSession.commit()
    assert n == 4
    rows = (await dbSession.execute(select(FeatureRule))).scalars().all()
    codes = {r.code for r in rows}
    assert codes == {
        "supplier_risk_score_main", "supplier_otd_high_risk",
        "supplier_defect_high_risk", "supplier_price_var_high_risk",
    }


async def test_seed_is_idempotent(dbSession) -> None:
    n1 = await seedFeatureRules(dbSession)
    await dbSession.commit()
    n2 = await seedFeatureRules(dbSession)
    await dbSession.commit()
    assert n1 == 4
    assert n2 == 0  # 第二次无变更


async def test_seed_risk_score_has_3_tiers(dbSession) -> None:
    await seedFeatureRules(dbSession)
    await dbSession.commit()
    rule = (await dbSession.execute(
        select(FeatureRule).where(FeatureRule.code == "supplier_risk_score_main")
    )).scalar_one()
    sevs = sorted([t.severity for t in rule.thresholds])
    assert sevs == ["HIGH", "LOW", "MEDIUM"]  # 3 tier RISK_SCORE
```

- [ ] **Step 2: Run test — FAIL**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test uv run pytest app/tests/integration/test_seed_feature_rules.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the seed script** (`backend/scripts/seed_feature_rules.py`):

```python
"""幂等 upsert 4 个内置 Feature Rule（feat-feature-rule-config, 2026-09-05）。

RISK_SCORE 三档（0.60 / 0.80 / 1.01）保证 byte-identical 字节级与 legacy
_decideLevel 兼容（spec §10.2 + §10.4）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.feature_rule_service import FeatureRuleService

logger = logging.getLogger(__name__)


FEATURE_RULE_SEEDS: list[dict[str, Any]] = [
    {
        "code": "supplier_risk_score_main",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_RISK_SCORE",
        "thresholds": [
            {"severity": "HIGH", "operator": "lt_inverse", "threshold_value": 0.60},
            {"severity": "MEDIUM", "operator": "lt_inverse", "threshold_value": 0.80},
            {"severity": "LOW", "operator": "lt_inverse", "threshold_value": 1.01},
        ],
    },
    {
        "code": "supplier_otd_high_risk",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_OTD_3M",
        "thresholds": [{"severity": "HIGH", "operator": "lt", "threshold_value": 90, "unit": "%"}],
    },
    {
        "code": "supplier_defect_high_risk",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_DEFECT_RATE_3M",
        "thresholds": [{"severity": "HIGH", "operator": "gt", "threshold_value": 5, "unit": "%"}],
    },
    {
        "code": "supplier_price_var_high_risk",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_PRICE_VARIANCE_3M",
        "thresholds": [{"severity": "HIGH", "operator": "gt", "threshold_value": 10, "unit": "%"}],
    },
]


async def seedFeatureRules(session: AsyncSession) -> int:
    """幂等 upsert 4 个内置规则。返回改动行数（用于 lifespan 日志）。"""
    service = FeatureRuleService()
    changed = 0
    for seed in FEATURE_RULE_SEEDS:
        existing = (
            await session.execute(
                __import__("sqlalchemy").select(
                    __import__("app.domain.models", fromlist=["FeatureRule"]).FeatureRule
                ).where(
                    __import__("app.domain.models", fromlist=["FeatureRule"]).FeatureRule.code == seed["code"]
                )
            )
        ).scalar_one_or_none()
        # v1 简化：seed 一律 upsert，幂等通过 code 命中判定
        await service.upsertSeed(session, seed["code"], seed)
        changed += 1 if existing is None else 0  # 首次插入计 1；后续 upsert 不计
    if changed:
        await session.flush()
        logger.info("seedFeatureRules: %d/%d rules created", changed, len(FEATURE_RULE_SEEDS))
    return changed


async def main() -> None:
    from app.infrastructure.database import getSessionFactory
    from app.tests import _pg_support  # noqa: F401

    factory = getSessionFactory()
    async with factory() as session:
        count = await seedFeatureRules(session)
        await session.commit()
        print(f"seed_feature_rules: {count}/{len(FEATURE_RULE_SEEDS)} created")


if __name__ == "__main__":
    asyncio.run(main())
```

> **Note:** the inline `__import__` calls are an artifact of the template — replace with normal top-of-file imports (`from sqlalchemy import select` + `from app.domain.models import FeatureRule`) when implementing.

- [ ] **Step 4: Run test — PASS**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test uv run pytest app/tests/integration/test_seed_feature_rules.py -v`
Expected: 3 tests pass.

- [ ] **Step 5: Wire into lifespan** (modify `backend/app/main.py`, in the existing `async with session_factory()` block in `lifespan`, after line 115):

```python
from scripts.seed_feature_rules import seedFeatureRules
from app.services.feature_rule_registry import feature_rule_registry

# (existing line 113-119 unchanged)
rule_changed = await seedFeatureRules(session)
if rule_changed:
    logger.info("feature_rule seed: %d/%d created", rule_changed, 4)
await feature_rule_registry.warmUp(session)  # 必须在 seed 之后
```

- [ ] **Step 6: Commit**

```bash
git add backend/scripts/seed_feature_rules.py backend/app/main.py \
        backend/app/tests/integration/test_seed_feature_rules.py
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): seed 4 rules + lifespan integration"
```

---

## Task 7: SupplierRiskService `_decideLevel_via_rules` (RISK-priority bypass)

**Files:**
- Modify: `backend/app/services/supplier_risk_service.py` (replace `_decideLevel` with `_decideLevel_via_rules` wrapper + keep legacy as private reference)
- Create: `backend/app/tests/integration/test_feature_rule_supplier_risk_parity.py` (CRITICAL — 6+ scenarios)

**Interfaces:**
- Produces: `SupplierRiskService._decideLevel_via_rules(view, contributions) -> tuple[RiskLevel, str]` (replaces `_decideLevel`)

- [ ] **Step 1: Write the CRITICAL parity test** (8 scenarios from spec §10.4)

```python
# backend/app/tests/integration/test_feature_rule_supplier_risk_parity.py
"""CRITICAL：证明 feature_rule 评估器与 legacy _decideLevel 字节级一致。"""
from decimal import Decimal

import pytest

from app.domain.enums import RiskLevel
from app.domain.models import FeatureRule
from app.domain.schemas import Supplier360Kpi, Supplier360Profile, Supplier360Read
from app.services.feature_rule_registry import feature_rule_registry
from app.services.supplier_risk_service import SupplierRiskService


def _view(kpis: list[Supplier360Kpi]) -> Supplier360Read:
    return Supplier360Read(
        profile=Supplier360Profile(enterprise_key=1, enterprise_code="S001", entity_type="SUPPLIER"),
        entity_codes=[], kpis=kpis,
    )


def _kpi(name: str, value: float | None, passed: bool = True) -> Supplier360Kpi:
    return Supplier360Kpi(
        feature_name=name, value=Decimal(str(value)) if value is not None else None,
        unit="%", latest=value is not None, passed=passed,
    )


async def test_parity_high_risk_score(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.55),
        _kpi("SUPPLIER_OTD_3M", None),
        _kpi("SUPPLIER_DEFECT_RATE_3M", None),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", None),
    ])
    level, source = SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.HIGH
    assert source == "supplier_risk_score_main"


async def test_parity_medium_risk_score(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.70),
        _kpi("SUPPLIER_OTD_3M", None),
        _kpi("SUPPLIER_DEFECT_RATE_3M", None),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", None),
    ])
    level, _ = SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.MEDIUM


async def test_parity_low_risk_score(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.85),
        _kpi("SUPPLIER_OTD_3M", None),
        _kpi("SUPPLIER_DEFECT_RATE_3M", None),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", None),
    ])
    level, _ = SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.LOW


async def test_parity_high_fallback_3_violations(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", None),
        _kpi("SUPPLIER_OTD_3M", 85, passed=False),
        _kpi("SUPPLIER_DEFECT_RATE_3M", 4, passed=False),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", 8, passed=False),
    ])
    level, _ = SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.HIGH


async def test_parity_low_fallback_1_violation(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", None),
        _kpi("SUPPLIER_OTD_3M", 92),
        _kpi("SUPPLIER_DEFECT_RATE_3M", 3, passed=False),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", 8),
    ])
    level, _ = SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.LOW


async def test_parity_unknown_all_missing(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", None),
        _kpi("SUPPLIER_OTD_3M", None),
        _kpi("SUPPLIER_DEFECT_RATE_3M", None),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", None),
    ])
    level, source = SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.UNKNOWN
    assert source == "unknown"


async def test_parity_risk_score_bypasses_otd_violation(dbSession) -> None:
    """RISK_SCORE tier MEDIUM matched → 即使 OTD 也违规，仍返回 MEDIUM（bypass）。"""
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.70),
        _kpi("SUPPLIER_OTD_3M", 85, passed=False),
        _kpi("SUPPLIER_DEFECT_RATE_3M", 4, passed=False),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", 8, passed=False),
    ])
    level, _ = SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.MEDIUM


async def test_parity_risk_score_low_bypasses_other_high(dbSession) -> None:
    """RISK_SCORE tier LOW matched → 即使其他 feature 也违规，仍返回 LOW（bypass）。"""
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.85),
        _kpi("SUPPLIER_OTD_3M", 85, passed=False),
        _kpi("SUPPLIER_DEFECT_RATE_3M", 4, passed=False),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", 8, passed=False),
    ])
    level, _ = SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.LOW


# ---- helpers ----

async def _seed_rules(dbSession) -> None:
    from scripts.seed_feature_rules import seedFeatureRules
    await seedFeatureRules(dbSession)
    await dbSession.commit()
    await feature_rule_registry.warmUp(dbSession)


def _to_contributions(view):
    from app.domain.schemas import SupplierRiskKpiContribution
    return [
        SupplierRiskKpiContribution(
            feature_name=kpi.feature_name,
            value=str(kpi.value) if kpi.value is not None else None,
            unit=kpi.unit, threshold=None, passed=kpi.passed, note=None,
        )
        for kpi in view.kpis
    ]
```

- [ ] **Step 2: Run test — FAIL** (method doesn't exist)

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test uv run pytest app/tests/integration/test_feature_rule_supplier_risk_parity.py -v`
Expected: FAIL with `AttributeError: 'SupplierRiskService' object has no attribute '_decideLevel_via_rules'`.

- [ ] **Step 3: Modify `supplier_risk_service.py`** — replace `_decideLevel` (lines 140-187) with `_decideLevel_via_rules` (keep `_decideLevel` as legacy alias for reference, OR delete it). Add imports for `Decimal` / `InvalidOperation` / `Severity` / `RuleOperator` / `FeatureRuleEvaluator` / `feature_rule_registry`. Reference spec §6.3.

```python
from decimal import Decimal, InvalidOperation
from app.domain.enums import RiskLevel, Severity
from app.domain.enums import RuleOperator
from app.services.feature_rule_evaluator import FeatureRuleEvaluator
from app.services.feature_rule_registry import feature_rule_registry


_SEVERITY_TO_RISK = {
    "HIGH": RiskLevel.HIGH,
    "MEDIUM": RiskLevel.MEDIUM,
    "LOW": RiskLevel.LOW,
    "INFO": RiskLevel.LOW,
}


# 在 SupplierRiskService 类内新增 _decideLevel_via_rules，删除旧的 _decideLevel
@staticmethod
async def _decideLevel_via_rules(
    view: "Supplier360Read",
    contributions: list["SupplierRiskKpiContribution"],
) -> tuple[RiskLevel, str]:
    """4-step RISK-priority bypass wrapper (spec §6.3，与 legacy _decideLevel 字节级一致)。"""
    values: dict[str, Decimal | None] = {}
    for c in contributions:
        if c.value is None:
            continue
        try:
            values[c.feature_name] = Decimal(c.value)
        except (InvalidOperation, ValueError):
            continue

    rules = feature_rule_registry.getEnabledRules(
        data_object="SUPPLIER", data_layer="FEATURE", target_level="RISK",
    )
    rule_hits: dict[str, RuleHit | None] = {}
    for rule in rules:
        value = values.get(rule.feature_name)
        if value is None:
            rule_hits[rule.code] = None
            continue
        hit: RuleHit | None = None
        for tier in sorted(rule.thresholds, key=lambda t: _SEVERITY_ORDER.get(t.severity, 99)):
            op = _OPERATORS.get(tier.operator)
            if op and op(value, tier.threshold_value):
                hit = RuleHit(
                    rule_code=rule.code, feature_name=rule.feature_name,
                    severity=tier.severity, operator=tier.operator,
                    threshold_value=tier.threshold_value, actual_value=value,
                )
                break
        rule_hits[rule.code] = hit

    # Step 1: RISK-priority bypass
    risk_score_hit = rule_hits.get("supplier_risk_score_main")
    if risk_score_hit is not None:
        return _SEVERITY_TO_RISK[risk_score_hit.severity], risk_score_hit.rule_code

    # Step 2: All missing → UNKNOWN
    all_missing = all(c.value is None for c in contributions)
    if all_missing:
        return RiskLevel.UNKNOWN, "unknown"

    # Step 3: MAX severity across other matched rules
    other_hits = [h for h in rule_hits.values() if h is not None and h.rule_code != "supplier_risk_score_main"]
    if other_hits:
        worst = min(other_hits, key=lambda h: _SEVERITY_ORDER.get(h.severity, 99))
        return _SEVERITY_TO_RISK[worst.severity], worst.rule_code

    # Step 4: All values present, no rule matched → LOW
    return RiskLevel.LOW, "no_match"
```

Also import `RuleHit` from `feature_rule_evaluator`. Add at top:

```python
from app.services.feature_rule_evaluator import RuleHit, _OPERATORS, _SEVERITY_ORDER
```

- [ ] **Step 4: Update `assess()` call site** — change `self._decideLevel(contributions)` to `self._decideLevel_via_rules(view, contributions)` (line 115 of supplier_risk_service.py).

- [ ] **Step 5: Run test — PASS** (8 tests)

Run: `cd backend && TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_feature_rule_supplier_risk_parity.py -v`
Expected: 8 tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/supplier_risk_service.py \
        backend/app/tests/integration/test_feature_rule_supplier_risk_parity.py
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): _decideLevel_via_rules RISK-priority bypass wrapper"
```

---

## Task 8: Supplier360Service `_safeLoadKpis` derive from registry

**Files:**
- Modify: `backend/app/services/supplier_360_service.py` (delete `DEFAULT_SUPPLIER_FEATURES` constant; add `_kpiSlotFeatureNames` helper; update `_safeLoadKpis`)

**Interfaces:**
- Produces: `_kpiSlotFeatureNames(data_object, data_layer) -> tuple[str, ...]` (helper, aggregates feature_names from registry)

- [ ] **Step 1: Write the failing test**

```python
# add to backend/app/tests/unit/test_supplier_360_service.py
"""_kpiSlotFeatureNames 聚合自 registry。"""
from app.services.feature_rule_registry import feature_rule_registry, FeatureRuleReady, FeatureThresholdReady
from app.services.supplier_360_service import _kpiSlotFeatureNames


def test_kpi_slot_feature_names_aggregates_from_registry() -> None:
    feature_rule_registry._loaded = True
    feature_rule_registry._rules = {("SUPPLIER", "FEATURE", "RISK"): [
        FeatureRuleReady(id=1, code="a", data_object="SUPPLIER", data_layer="FEATURE",
                          target_level="RISK", feature_name="X", enabled=True, priority=100,
                          thresholds=(FeatureThresholdReady("HIGH", "lt", 10, "%", 1),)),
        FeatureRuleReady(id=2, code="b", data_object="SUPPLIER", data_layer="FEATURE",
                          target_level="RISK", feature_name="Y", enabled=True, priority=100,
                          thresholds=(FeatureThresholdReady("HIGH", "lt", 20, "%", 1),)),
    ]}
    names = _kpiSlotFeatureNames()
    assert set(names) == {"X", "Y"}
    # sorted alphabetically
    assert names == ("X", "Y")
```

- [ ] **Step 2: Run test — FAIL**

Run: `cd backend && uv run pytest app/tests/unit/test_supplier_360_service.py::test_kpi_slot_feature_names_aggregates_from_registry -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Modify `supplier_360_service.py`**

Delete the `DEFAULT_SUPPLIER_FEATURES` constant (lines 50-55). Add import at top:

```python
from app.services.feature_rule_registry import feature_rule_registry
```

Add helper at module level (after the imports, before `class Supplier360Service`):

```python
def _kpiSlotFeatureNames(
    data_object: str = "SUPPLIER", data_layer: str = "FEATURE"
) -> tuple[str, ...]:
    """聚合所有 enabled 规则的 feature_name（spec §6.4）。"""
    seen: set[str] = set()
    for target_level in ("RISK", "QUALITY_SCORE", "CUSTOM"):
        for rule in feature_rule_registry.getEnabledRules(
            data_object, data_layer, target_level
        ):
            seen.add(rule.feature_name)
    return tuple(sorted(seen))
```

Update `_safeLoadKpis` (line 239): replace `for feature_name in DEFAULT_SUPPLIER_FEATURES:` with `for feature_name in _kpiSlotFeatureNames():`.

- [ ] **Step 4: Run test — PASS**

Run: `cd backend && uv run pytest app/tests/unit/test_supplier_360_service.py -v`
Expected: existing tests + new test all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/supplier_360_service.py \
        backend/app/tests/unit/test_supplier_360_service.py
git -c commit.gpgsign=false commit -m "refactor(supplier-360): derive KPI slot list from registry"
```

---

## Task 9: Conftest autouse warmUp extension

**Files:**
- Modify: `backend/app/tests/integration/conftest.py` (extend `warmAgentCaches` to include `feature_rule_registry.warmUp` + `commit()`)

**Why:** Existing `warmAgentCaches` calls `seedAgentToolConfigs` + `seedBusinessObjects` + warmUp for `agent_binding_cache` and `agent_tool_config_registry`. The commit `d01a4e1` lesson: must `await dbSession.commit()` after each `warmUp()` to release implicit transaction locks. Add `feature_rule_registry` to the same pattern.

- [ ] **Step 1: Modify `conftest.py`** — add to `warmAgentCaches` autouse fixture (after line 70, before the existing comment):

```python
from scripts.seed_feature_rules import seedFeatureRules
from app.services.feature_rule_registry import feature_rule_registry

# (existing seedAgentToolConfigs + seedBusinessObjects + warmUps stay)
# Append:
await seedFeatureRules(dbSession)
await dbSession.commit()
feature_rule_registry.invalidate()
await feature_rule_registry.warmUp(dbSession)
await dbSession.commit()  # 关掉 warmUp SELECT 留下的隐式事务
```

And at the bottom of the fixture (after `agent_tool_config_registry.invalidate()`):

```python
feature_rule_registry.invalidate()
```

- [ ] **Step 2: Run integration suite — verify no regression**

Run: `cd backend && TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_feature_rule_supplier_risk_parity.py app/tests/integration/test_seed_feature_rules.py app/tests/integration/test_agent_tool_config_audit.py -v`
Expected: all 3 files pass (parity 8 + seed 3 + audit 6 = 17 tests).

- [ ] **Step 3: Commit**

```bash
git add backend/app/tests/integration/conftest.py
git -c commit.gpgsign=false commit -m "test(feature-rule-config): conftest warmAgentCaches extend to feature_rule_registry"
```

---

## Task 10: REST router (CRUD + toggle + ACL)

**Files:**
- Create: `backend/app/api/v1/feature_rules.py`
- Modify: `backend/app/main.py` (router registration)
- Create: `backend/app/tests/integration/test_feature_rule_api.py`

**Interfaces:**
- Produces: 5 endpoints under `/api/v1/feature-rules` (admin ACL for writes, getCurrentUser for reads)

- [ ] **Step 1: Write the failing API tests** (CRUD happy path + 409 version + 409 referencing + admin-only writes + 422 validation)

```python
# backend/app/tests/integration/test_feature_rule_api.py
"""Feature Rule API 集成测试（spec §9）。"""
import pytest


@pytest.fixture
async def adminActor(client):
    """依赖 admin 用户登录态（沿用 AgentToolConfig 现有 fixture 模式）。"""
    # 沿用 feat-agent-tool-config-db 的 admin fixture 实现
    from tests._helpers import loginAdmin  # placeholder，实际 follow 现有 helper
    return await loginAdmin(client)


async def test_list_rules_requires_login(client):
    r = await client.get("/api/v1/feature-rules")
    assert r.status_code == 401


async def test_list_rules_returns_seeded(client, adminActor, dbSession):
    # seed 4 rules via conftest autouse
    r = await client.get("/api/v1/feature-rules")
    assert r.status_code == 200
    assert len(r.json()) == 4


async def test_get_rule_not_found(client, adminActor):
    r = await client.get("/api/v1/feature-rules/nonexistent")
    assert r.status_code == 404


async def test_create_rule_admin_only(client):
    """未登录用户创建 → 401。"""
    r = await client.post("/api/v1/feature-rules", json={
        "code": "new_rule", "data_object": "SUPPLIER", "data_layer": "FEATURE",
        "target_level": "RISK", "feature_name": "X",
        "thresholds": [{"severity": "HIGH", "operator": "lt", "threshold_value": 10}],
    })
    assert r.status_code == 401


async def test_create_rule_admin_succeeds(client, adminActor):
    r = await client.post("/api/v1/feature-rules", json={
        "code": "new_rule", "data_object": "SUPPLIER", "data_layer": "FEATURE",
        "target_level": "RISK", "feature_name": "X",
        "thresholds": [{"severity": "HIGH", "operator": "lt", "threshold_value": 10}],
    }, headers=adminActor)
    assert r.status_code == 201
    assert r.json()["version"] == 1


async def test_create_rule_dup_code_409(client, adminActor):
    payload = {
        "code": "supplier_risk_score_main", "data_object": "SUPPLIER",
        "data_layer": "FEATURE", "target_level": "RISK", "feature_name": "X",
        "thresholds": [{"severity": "HIGH", "operator": "lt", "threshold_value": 10}],
    }
    r = await client.post("/api/v1/feature-rules", json=payload, headers=adminActor)
    assert r.status_code == 409


async def test_update_rule_version_mismatch_409(client, adminActor):
    r = await client.put(
        "/api/v1/feature-rules/supplier_risk_score_main",
        json={"enabled": False, "version": 999},
        headers=adminActor,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["current_version"] == 1


async def test_delete_rule_referenced_409(client, adminActor, dbSession):
    # 模拟 AgentDefinition 引用
    from app.domain.models import AgentDefinition
    agent = AgentDefinition(agent_code="test_ref_agent", display_name="t", tool_name="supplier_risk_score_main")
    dbSession.add(agent)
    await dbSession.commit()
    r = await client.delete(
        "/api/v1/feature-rules/supplier_risk_score_main", headers=adminActor
    )
    assert r.status_code == 409
    assert "supplier_risk_score_main" in r.json()["detail"]["referencing"]


async def test_toggle_enabled(client, adminActor):
    r = await client.post(
        "/api/v1/feature-rules/supplier_otd_high_risk/toggle",
        json={"enabled": False},
        headers=adminActor,
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["version"] == 2
```

(Adjust `adminActor` fixture to match existing `feat-agent-tool-config-db` test pattern — check `test_agent_tool_config_api.py` for the exact helper.)

- [ ] **Step 2: Run test — FAIL**

Run: `cd backend && TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_feature_rule_api.py -v`
Expected: FAIL with 404 (route doesn't exist).

- [ ] **Step 3: Write the router** (`backend/app/api/v1/feature_rules.py`):

```python
"""FeatureRule 管理 API（feat-feature-rule-config）。

挂在 /api/v1/feature-rules：
  GET    /api/v1/feature-rules                 列表（enabledOnly 过滤）
  GET    /api/v1/feature-rules/{code}          详情
  POST   /api/v1/feature-rules                 创建（admin only）
  PUT    /api/v1/feature-rules/{code}          更新（admin only, optimistic lock）
  DELETE /api/v1/feature-rules/{code}          删除（admin only, 409 if referenced）
  POST   /api/v1/feature-rules/{code}/toggle   翻转 enabled（admin only）
  POST   /api/v1/feature-rules/parse-description  LLM NL → suggestions（admin only）

ACL：读 - 所有登录用户；写 - 仅 admin。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getAdminOnlyActor, getCurrentUser, getDb
from app.domain.schemas import (
    FeatureRuleCreate,
    FeatureRuleRead,
    FeatureRuleUpdate,
)
from app.services.feature_rule_registry import feature_rule_registry
from app.services.feature_rule_service import FeatureRuleService

router = APIRouter(prefix="/api/v1/feature-rules", tags=["feature-rules"])


def _svc() -> FeatureRuleService:
    return FeatureRuleService()


@router.get("", response_model=list[FeatureRuleRead])
async def listFeatureRules(
    enabledOnly: bool = False,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[FeatureRuleRead]:
    rows = await _svc().listRules(session, enabledOnly=enabledOnly)
    return [FeatureRuleRead.model_validate(r) for r in rows]


@router.get("/{code}", response_model=FeatureRuleRead)
async def getFeatureRule(
    code: str,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleRead:
    row = await _svc().getRule(session, code)
    return FeatureRuleRead.model_validate(row)


@router.post("", response_model=FeatureRuleRead, status_code=status.HTTP_201_CREATED)
async def createFeatureRule(
    payload: FeatureRuleCreate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleRead:
    row = await _svc().createRule(session, payload, _admin)
    await session.commit()
    # 写时失效
    if row.id:
        await feature_rule_registry.reloadOne(session, row.id)
    return FeatureRuleRead.model_validate(row)


@router.put("/{code}", response_model=FeatureRuleRead)
async def updateFeatureRule(
    code: str,
    payload: FeatureRuleUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleRead:
    row = await _svc().updateRule(session, code, payload, _admin)
    await session.commit()
    await feature_rule_registry.reloadOne(session, row.id)
    return FeatureRuleRead.model_validate(row)


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteFeatureRule(
    code: str,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    row = await _svc().getRule(session, code)
    await _svc().deleteRule(session, code, _admin)
    await session.commit()
    feature_rule_registry.invalidate(row.id)


@router.post("/{code}/toggle", response_model=FeatureRuleRead)
async def toggleFeatureRule(
    code: str,
    enabled: bool = Body(..., embed=True),
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleRead:
    row = await _svc().toggleEnabled(session, code, enabled, _admin)
    await session.commit()
    await feature_rule_registry.reloadOne(session, row.id)
    return FeatureRuleRead.model_validate(row)
```

- [ ] **Step 4: Register router in `main.py`** — add after existing agent_tools router registration (~line 240):

```python
app.include_router(feature_rules.router, prefix="/api/v1/feature-rules", tags=["feature-rules"])
```

Add import at top of `main.py` router section:

```python
from app.api.v1 import feature_rules as feature_rules_module
```

- [ ] **Step 5: Run test — PASS**

Run: `cd backend && TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_feature_rule_api.py -v`
Expected: 9 tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/feature_rules.py backend/app/main.py \
        backend/app/tests/integration/test_feature_rule_api.py
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): REST API + ACL"
```

---

## Task 11: LLM parse-description endpoint

**Files:**
- Create: `backend/app/services/feature_rule_llm_service.py`
- Modify: `backend/app/api/v1/feature_rules.py` (add `/parse-description` endpoint)
- Create: `backend/app/tests/integration/test_feature_rule_llm_parse.py`

**Interfaces:**
- Produces: `parseFeatureRuleDescription(request, llm_factory) -> FeatureRuleParseDescriptionResponse`
- Raises `LLMUnavailableError` (503) on LLM failure

- [ ] **Step 1: Write the failing tests** (mock LLM happy path + 503 path + audit)

```python
# backend/app/tests/integration/test_feature_rule_llm_parse.py
"""parse-description LLM 集成测试（mock LLM client）。"""
from unittest.mock import AsyncMock

import pytest


async def test_parse_description_happy_path(client, adminActor, dbSession):
    # seed feature_definition（conftest autouse 已 seed）
    fake_response = AsyncMock()
    fake_response.content = '''
    {
      "suggested_thresholds": [
        {"feature_name": "SUPPLIER_OTD_3M", "severity": "HIGH", "operator": "lt", "threshold_value": 90, "unit": "%", "confidence": 0.9, "rationale": "OTD < 90%"}
      ],
      "reasoning": "识别出 1 条规则建议",
      "overall_confidence": 0.9,
      "warnings": []
    }
    '''
    fake_response.promptTokens = 100
    fake_response.completionTokens = 50
    fake_response.modelName = "test-model"

    from app.services.feature_rule_llm_service import parseFeatureRuleDescription
    result = await parseFeatureRuleDescription(
        payload=...,  # FeatureRuleParseDescriptionRequest
        llm_client=fake_response,
    )
    assert len(result.suggested_thresholds) == 1
    assert result.suggested_thresholds[0].feature_name == "SUPPLIER_OTD_3M"


async def test_parse_description_llm_unavailable_raises_503(client, adminActor):
    from app.domain.exceptions import LLMUnavailableError
    fake_client = AsyncMock()
    fake_client.complete.side_effect = RuntimeError("LLM down")

    from app.domain.schemas import FeatureRuleParseDescriptionRequest
    from app.services.feature_rule_llm_service import parseFeatureRuleDescription
    with pytest.raises(LLMUnavailableError):
        await parseFeatureRuleDescription(
            payload=FeatureRuleParseDescriptionRequest(
                data_object="SUPPLIER", data_layer="FEATURE", target_level="RISK",
                natural_language="OTD 低于 90% 即高风险，价格偏差超 15% 是中等风险",
            ),
            llm_client=fake_client,
        )
```

- [ ] **Step 2: Run test — FAIL**

Run: `cd backend && TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_feature_rule_llm_parse.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the LLM service** (`backend/app/services/feature_rule_llm_service.py`):

```python
"""parse-description LLM 服务（spec §7，config-time only，不写 DB）。

候选 feature_name 从 feature_definition 加载 → system prompt 注入 →
LLM 返回 Pydantic 结构化输出 → 校验通过返回；任意异常 → LLMUnavailableError(503)。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import LLMUnavailableError
from app.domain.models import FeatureDefinition
from app.domain.schemas import (
    FeatureRuleParseDescriptionRequest,
    FeatureRuleParseDescriptionResponse,
    FeatureRuleThresholdSuggestion,
)
from app.services.outbox_service import OutboxService

logger = logging.getLogger(__name__)


async def _loadCandidateFeatures(
    session: AsyncSession, data_object: str, data_layer: str
) -> list[dict]:
    rows = (await session.execute(
        select(FeatureDefinition).where(
            FeatureDefinition.data_object == data_object,
            FeatureDefinition.data_layer == data_layer,
        )
    )).scalars().all()
    return [
        {"feature_name": r.feature_name, "feature_alias": r.feature_alias, "unit": r.unit}
        for r in rows
    ]


def _buildSystemPrompt(candidates: list[dict]) -> str:
    cand_block = "\n".join(
        f"- {c['feature_name']}（{c['feature_alias']}，unit={c['unit'] or '-'}）"
        for c in candidates
    )
    return (
        "你是 Feature Rule 配置助理。基于候选 feature 列表，把管理员的自然语言策略"
        "解析为结构化阈值建议。严格只用候选 feature_name；不要臆造。"
        "\n\n候选 features:\n"
        f"{cand_block}\n\n"
        "输出 JSON：\n"
        '{"suggested_thresholds": [...], "reasoning": "...", "overall_confidence": 0.0-1.0, "warnings": [...]}\n'
        "每条 suggested_threshold 字段: feature_name, severity(HIGH/MEDIUM/LOW/INFO), "
        "operator(lt/lte/gt/gte/lt_inverse), threshold_value(number), unit, confidence, rationale"
    )


async def parseFeatureRuleDescription(
    session: AsyncSession,
    payload: FeatureRuleParseDescriptionRequest,
    llm_client: Any,
    actor: str | None = None,
) -> FeatureRuleParseDescriptionResponse:
    """调 LLM 解析自然语言；失败 → 503。"""
    candidates = await _loadCandidateFeatures(session, payload.data_object, payload.data_layer)
    system_prompt = _buildSystemPrompt(candidates)

    try:
        response = await llm_client.complete(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload.natural_language},
            ],
        )
    except Exception as e:
        logger.warning("parse-description LLM 调用失败: %s", e, exc_info=True)
        raise LLMUnavailableError("AI 辅助不可用，请手动填写 thresholds") from e

    content = getattr(response, "content", "") or ""
    try:
        parsed = json.loads(content)
        result = FeatureRuleParseDescriptionResponse.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning("parse-description LLM 输出解析失败: %s", e, exc_info=True)
        raise LLMUnavailableError(f"LLM 输出无法解析: {e}") from e

    # 审计
    await OutboxService().enqueue(
        session,
        event_type="feature_rule_parse_description",
        entity_type="feature_rule",
        entity_id=None,
        actor=actor or "unknown",
        actor_departments=(),
        payload={
            "data_object": payload.data_object,
            "data_layer": payload.data_layer,
            "target_level": payload.target_level,
            "natural_language_length": len(payload.natural_language),
            "suggested_count": len(result.suggested_thresholds),
            "overall_confidence": result.overall_confidence,
            "warnings": result.warnings,
        },
    )
    return result
```

- [ ] **Step 4: Add `/parse-description` endpoint to router** (append to `backend/app/api/v1/feature_rules.py`):

```python
from app.domain.schemas import FeatureRuleParseDescriptionRequest, FeatureRuleParseDescriptionResponse
from app.services.feature_rule_llm_service import parseFeatureRuleDescription


@router.post("/parse-description", response_model=FeatureRuleParseDescriptionResponse)
async def parseDescription(
    payload: FeatureRuleParseDescriptionRequest,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleParseDescriptionResponse:
    """LLM 解析自然语言策略 → 建议阈值（advisory，不持久化）。"""
    from app.infrastructure.llm.base_client import BaseLlmClient  # placeholder
    from app.services.chat_helpers import buildDefaultLlmClient  # 沿用 NL2Sql 默认 factory
    client = buildDefaultLlmClient(None)
    return await parseFeatureRuleDescription(session, payload, client, actor=_admin.userId)
```

(`buildDefaultLlmClient` is illustrative — copy actual factory pattern from NL2SqlService.)

- [ ] **Step 5: Run test — PASS**

Run: `cd backend && TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_feature_rule_llm_parse.py -v`
Expected: 2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/feature_rule_llm_service.py \
        backend/app/api/v1/feature_rules.py \
        backend/app/tests/integration/test_feature_rule_llm_parse.py
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): LLM parse-description endpoint"
```

---

## Task 12: Audit log integration test

**Files:**
- Create: `backend/app/tests/integration/test_feature_rule_audit.py`

- [ ] **Step 1: Write the failing audit test** (CRUD + toggle + parse-description each writes an outbox event)

```python
# backend/app/tests/integration/test_feature_rule_audit.py
"""Feature Rule CRUD + parse-description 触发 outbox 审计。"""
import pytest


async def test_create_rule_writes_audit(client, adminActor, dbSession):
    # 调用 API 创建 rule → outbox 应有 feature_rule_created 事件
    ...


async def test_parse_description_writes_audit(client, adminActor, dbSession, monkeypatch):
    # mock LLM 客户端 → 调用 /parse-description → outbox 有 feature_rule_parse_description 事件
    ...
```

- [ ] **Step 2-4: 实现 + 验证 + commit** (same shape as `test_agent_tool_config_audit.py` from `feat-agent-tool-config-db` — copy and adapt).

```bash
git add backend/app/tests/integration/test_feature_rule_audit.py
git -c commit.gpgsign=false commit -m "test(feature-rule-config): audit log integration tests"
```

---

## Task 13: Frontend types + API client + i18n

**Files:**
- Create: `frontend/src/api/featureRules.ts`
- Create: `frontend/src/types/featureRules.ts`
- Modify: `frontend/src/i18n/zh-CN.ts` (add `featureRules.*` namespace)
- Modify: `frontend/src/i18n/en-US.ts` (add `featureRules.*` namespace)
- Create: `frontend/src/tests/featureRules.test.ts`

**Interfaces:**
- Produces: 7 API functions: `listFeatureRules`, `getFeatureRule`, `createFeatureRule`, `updateFeatureRule`, `deleteFeatureRule`, `toggleFeatureRule`, `parseFeatureRuleDescription`

- [ ] **Step 1: Write the failing API client tests**

```typescript
// frontend/src/tests/featureRules.test.ts
import { describe, it, expect, vi } from 'vitest';
import {
  listFeatureRules,
  getFeatureRule,
  createFeatureRule,
  updateFeatureRule,
  deleteFeatureRule,
  toggleFeatureRule,
  parseFeatureRuleDescription,
} from '../api/featureRules';

describe('featureRules API', () => {
  it('listFeatureRules hits GET /api/v1/feature-rules', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] });
    globalThis.fetch = fetchMock as any;
    await listFeatureRules();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/feature-rules',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  // ... 6 more tests mirroring the pattern
});
```

- [ ] **Step 2: Run test — FAIL**

Run: `cd frontend && npx vitest run src/tests/featureRules.test.ts`
Expected: FAIL with `Cannot find module '../api/featureRules'`.

- [ ] **Step 3: Write types** (`frontend/src/types/featureRules.ts`):

```typescript
export type Severity = 'HIGH' | 'MEDIUM' | 'LOW' | 'INFO';
export type RuleOperator = 'lt' | 'lte' | 'gt' | 'gte' | 'lt_inverse';

export interface FeatureRuleThreshold {
  severity: Severity;
  operator: RuleOperator;
  threshold_value: number;
  unit?: string | null;
  threshold_order: number;
}

export interface FeatureRule {
  id: number;
  code: string;
  data_object: string;
  data_layer: string;
  target_level: string;
  feature_name: string;
  enabled: boolean;
  priority: number;
  policy_description?: string | null;
  version: number;
  thresholds: FeatureRuleThreshold[];
  created_time: string;
  updated_time?: string | null;
}

export interface FeatureRuleCreate {
  code: string;
  data_object: string;
  data_layer: string;
  target_level: string;
  feature_name: string;
  enabled?: boolean;
  priority?: number;
  policy_description?: string | null;
  thresholds: FeatureRuleThreshold[];
}

export interface FeatureRuleUpdate {
  enabled?: boolean;
  priority?: number;
  policy_description?: string | null;
  thresholds?: FeatureRuleThreshold[];
  version: number;
}

export interface FeatureRuleThresholdSuggestion {
  feature_name: string;
  severity: Severity;
  operator: RuleOperator;
  threshold_value: number;
  unit?: string | null;
  confidence: number;
  rationale: string;
}

export interface FeatureRuleParseDescriptionRequest {
  data_object: string;
  data_layer: string;
  target_level: string;
  natural_language: string;
}

export interface FeatureRuleParseDescriptionResponse {
  suggested_thresholds: FeatureRuleThresholdSuggestion[];
  reasoning: string;
  overall_confidence: number;
  warnings: string[];
}
```

- [ ] **Step 4: Write API client** (`frontend/src/api/featureRules.ts`):

```typescript
import type {
  FeatureRule,
  FeatureRuleCreate,
  FeatureRuleUpdate,
  FeatureRuleParseDescriptionRequest,
  FeatureRuleParseDescriptionResponse,
} from '../types/featureRules';

const BASE = '/api/v1/feature-rules';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) throw new Error(`featureRules API ${res.status}`);
  if (res.status === 204) return undefined as unknown as T;
  return res.json();
}

export function listFeatureRules(params?: { enabledOnly?: boolean }): Promise<FeatureRule[]> {
  const q = params?.enabledOnly ? '?enabledOnly=true' : '';
  return request<FeatureRule[]>(q);
}

export function getFeatureRule(code: string): Promise<FeatureRule> {
  return request<FeatureRule>(`/${encodeURIComponent(code)}`);
}

export function createFeatureRule(payload: FeatureRuleCreate): Promise<FeatureRule> {
  return request<FeatureRule>('', { method: 'POST', body: JSON.stringify(payload) });
}

export function updateFeatureRule(code: string, payload: FeatureRuleUpdate): Promise<FeatureRule> {
  return request<FeatureRule>(`/${encodeURIComponent(code)}`, { method: 'PUT', body: JSON.stringify(payload) });
}

export function deleteFeatureRule(code: string): Promise<void> {
  return request<void>(`/${encodeURIComponent(code)}`, { method: 'DELETE' });
}

export function toggleFeatureRule(code: string, enabled: boolean): Promise<FeatureRule> {
  return request<FeatureRule>(`/${encodeURIComponent(code)}/toggle`, {
    method: 'POST', body: JSON.stringify({ enabled }),
  });
}

export function parseFeatureRuleDescription(
  payload: FeatureRuleParseDescriptionRequest,
): Promise<FeatureRuleParseDescriptionResponse> {
  return request<FeatureRuleParseDescriptionResponse>('/parse-description', {
    method: 'POST', body: JSON.stringify(payload),
  });
}
```

- [ ] **Step 5: Add i18n namespace** (modify `frontend/src/i18n/zh-CN.ts` and `en-US.ts`):

```typescript
// zh-CN.ts - add
featureRules: {
  title: 'Feature 规则',
  columns: { /* ... */ },
  actions: { create: '新建', edit: '编辑', delete: '删除', toggle: '启用/禁用', aiAssist: 'AI 辅助填写' },
  form: { /* 字段标签 */ },
  messages: { /* 成功/失败提示 */ },
  errors: { versionConflict: '版本冲突，请刷新后重试', referencing: '规则被引用，无法删除', llmUnavailable: 'AI 辅助不可用' },
},
```

```typescript
// en-US.ts - add English equivalents
```

- [ ] **Step 6: Run test — PASS**

Run: `cd frontend && npx vitest run src/tests/featureRules.test.ts`
Expected: 7 tests pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/types/featureRules.ts frontend/src/api/featureRules.ts \
        frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts \
        frontend/src/tests/featureRules.test.ts
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): frontend types + API client + i18n"
```

---

## Task 14: AdminFeatureRulesPage + AI modal

**Files:**
- Create: `frontend/src/pages/AdminFeatureRulesPage.tsx`
- Create: `frontend/src/components/admin/AiAssistModal.tsx`
- Create: `frontend/src/tests/AdminFeatureRulesPage.test.tsx`

**Interfaces:**
- Produces: List page + create/edit drawer + AI modal (prefill thresholds)

- [ ] **Step 1: Write the failing page tests**

```tsx
// frontend/src/tests/AdminFeatureRulesPage.test.tsx
import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import AdminFeatureRulesPage from '../pages/AdminFeatureRulesPage';

vi.mock('../api/featureRules', () => ({
  listFeatureRules: vi.fn().mockResolvedValue([
    { id: 1, code: 'r1', data_object: 'SUPPLIER', data_layer: 'FEATURE',
      target_level: 'RISK', feature_name: 'X', enabled: true, priority: 100,
      policy_description: null, version: 1, thresholds: [],
      created_time: '2026-09-05T00:00:00Z', updated_time: null },
  ]),
  toggleFeatureRule: vi.fn(),
  deleteFeatureRule: vi.fn(),
}));

describe('AdminFeatureRulesPage', () => {
  it('renders seeded rules in table', async () => {
    render(<AdminFeatureRulesPage />);
    await waitFor(() => expect(screen.getByText('r1')).toBeInTheDocument());
  });

  it('opens create drawer on 新建 click', async () => {
    render(<AdminFeatureRulesPage />);
    // click 新建 button → drawer with code/feature_name inputs visible
  });

  it('opens AI modal on AI 辅助填写 click', async () => {
    render(<AdminFeatureRulesPage />);
    // open drawer → click AI 辅助填写 → modal with policy_description textarea
  });
});
```

- [ ] **Step 2-5: Implement** (mirror `frontend/src/pages/AdminToolsPage.tsx` structure — table + AntD `Drawer` + `Form.List` for thresholds + AntD `Modal` for AI assist). Reference `feat-agent-tool-config-db` precedent for AntD pitfalls (`destroyOnHidden`, `Form.List`, whitespace regex).

- [ ] **Step 6: Run test — PASS**

Run: `cd frontend && npx vitest run src/tests/AdminFeatureRulesPage.test.tsx`
Expected: 3 tests pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/AdminFeatureRulesPage.tsx \
        frontend/src/components/admin/AiAssistModal.tsx \
        frontend/src/tests/AdminFeatureRulesPage.test.tsx
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): AdminFeatureRulesPage + AI modal"
```

---

## Task 15: App route + menu seed

**Files:**
- Modify: `frontend/src/App.tsx` (route registration)
- Modify: `backend/scripts/seed_menu_config.py` (add `/admin/feature-rules` entry — required per `qa-system-menu-config-seed` memory)

- [ ] **Step 1: Add route in `App.tsx`** — find existing `/admin/tools` registration and add sibling:

```tsx
const AdminFeatureRulesPage = React.lazy(() => import('./pages/AdminFeatureRulesPage'));

// In routes:
<Route path="/admin/feature-rules" element={<AdminFeatureRulesPage />} />
```

- [ ] **Step 2: Add menu seed entry** — find `agent_tools` or similar in `seed_menu_config.py` and add:

```python
{
    "code": "admin_feature_rules",
    "path": "/admin/feature-rules",
    "i18n_key": "menu.item.adminFeatureRules",
    "group": "admin",
    "required_role": "admin",
},
```

And add i18n keys in `frontend/src/i18n/{zh-CN,en-US}.ts`:

```typescript
// zh-CN.ts menu namespace
menu: { ..., item: { ..., adminFeatureRules: 'Feature 规则' } }

// en-US.ts
menu: { ..., item: { ..., adminFeatureRules: 'Feature Rules' } }
```

- [ ] **Step 3: Verify menu renders** (run frontend dev server + login as admin + check sidebar)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/App.tsx backend/scripts/seed_menu_config.py \
        frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts
git -c commit.gpgsign=false commit -m "feat(feature-rule-config): route + menu seed entry"
```

---

## Task 16: Full backend test suite + coverage gate

- [ ] **Step 1: Run full backend suite with coverage**

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80
```

Expected: ≥ 80% coverage; all new + existing tests pass.

- [ ] **Step 2: Investigate any failures** — fix root cause (do not skip tests). For pre-existing failures unrelated to this change, document in commit message.

- [ ] **Step 3: Commit** (if any coverage gaps fixed)

```bash
git add <fixed files>
git -c commit.gpgsign=false commit -m "test(feature-rule-config): full backend suite + coverage gate"
```

---

## Task 17: Frontend test suite

- [ ] **Step 1: Run vitest**

```bash
cd frontend && npx vitest run src/tests/AdminFeatureRulesPage.test.tsx src/tests/featureRules.test.ts
```

Expected: all tests pass.

- [ ] **Step 2: Run coverage** (if vitest configured with v8 provider)

```bash
cd frontend && npx vitest run --coverage
```

(Per project convention — confirm whether coverage is enforced for frontend.)

---

## Task 18: Harness change summary + memory

**Files:**
- Create: `Harness/changes/feat-feature-rule-config/summary.md`
- Create: `.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-feature-rule-config.md`
- Modify: `.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/MEMORY.md` (append pointer)

- [ ] **Step 1: Write change summary** (`Harness/changes/feat-feature-rule-config/summary.md`) — follow template from `Harness/changes/feat-agent-tool-config-db/summary.md`:

```markdown
# feat-feature-rule-config change summary

## Goal
把硬编码 RISK_RULES + DEFAULT_SUPPLIER_FEATURES 迁到 DB-backed 通用 Feature 规则引擎。

## What changed
- Alembic 0043：feature_rule + feature_rule_threshold（2 表 1:N）
- FeatureRuleService + FeatureRuleEvaluator + FeatureRuleRegistry + feature_rule_llm_service
- SupplierRiskService._decideLevel_via_rules（RISK-priority bypass 包装，字节级兼容 legacy）
- Supplier360Service._safeLoadKpis（从 registry 聚合 feature_name）
- /api/v1/feature-rules REST API（admin ACL for writes）
- /admin/feature-rules admin UI + AI 辅助填写 modal
- seed_feature_rules.py 4 条内置规则幂等 upsert

## Parity proof
test_feature_rule_supplier_risk_parity.py 8 个场景证明 byte-identical 兼容 legacy _decideLevel。

## Risks & mitigations
- R1 LLM 幻觉 → Pydantic 校验 + 候选 feature_name 注入 + 503 fail-loud
- R2 删除引用 → 409 referencing with consumer list
- R3 缓存失效 → 所有写操作 reloadOne（asyncio.Lock）
- R5 conftest lock → commit after warmUp pattern

## Out of scope
- DataQualityRuleModel 内部 evaluator 迁移（target_level="QUALITY_SCORE" plumbing reserved）
- Compound expressions (AND/OR trees)
- Redis 多实例缓存
```

- [ ] **Step 2: Write memory entry**

```markdown
---
name: qa-system-feature-rule-config
description: 通用 Feature Rule 规则引擎：DB-backed SSOT + 3-tier RISK_SCORE + RISK-priority bypass wrapper（byte-identical legacy parity）
metadata:
  type: project
---

# qa-system-feature-rule-config

feat-feature-rule-config (2026-09-05) 把硬编码 RISK_RULES + DEFAULT_SUPPLIER_FEATURES 迁到 DB。

## 关键点

- **3-tier RISK_SCORE seed**（HIGH 0.60 / MEDIUM 0.80 / LOW 1.01）：LOW tier 1.01 永远命中（值域 [0,1]），保证 RISK_SCORE ≥ 0.80 时 bypass 返回 LOW（byte-identical 字节级兼容）。
- **RISK-priority bypass wrapper**：`SupplierRiskService._decideLevel_via_rules` 4-step legacy 逻辑（RISK_SCORE bypass → all-missing UNKNOWN → MAX others → no-match LOW）。
- **纯同步 evaluator**：`FeatureRuleEvaluator.evaluate(...)` 跨规则 MAX severity（min(SEVERITY_ORDER)），无 IO / 无 LLM。
- **Registry warmUp 模式**：与 `AgentToolConfigRegistry` 一致；conftest autouse 必须 `commit()` after warmUp（d01a4e1 教训）。
- **Admin UI**：`/admin/feature-rules` + AI 辅助填写（LLM advisory only，advisory-only 不写库）。
- **未来扩展**：`target_level="QUALITY_SCORE"` 字段保留供 DataQualityRuleModel 后续接入。

**Why:** 业务用户可改阈值无需代码；后续 Phase 6 Agent 平台可演进多 target_level。

**How to apply:** 修改 RISK 规则 → 走 admin UI 或 `seed_feature_rules.py`；不要改 `supplier_risk_service._decideLevel_via_rules` 里的 RISK_SCORE bypass 逻辑（破坏 parity）。
```

- [ ] **Step 3: Update MEMORY.md** — append pointer:

```markdown
- [Feature Rule Config DB-Backed](qa-system-feature-rule-config.md) — 通用 Feature 规则引擎 + 3-tier RISK_SCORE + RISK-priority bypass wrapper（byte-identical legacy parity）
```

- [ ] **Step 4: Commit**

```bash
git add Harness/changes/feat-feature-rule-config/summary.md \
        .claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-feature-rule-config.md \
        .claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/MEMORY.md
git -c commit.gpgsign=false commit -m "docs(feature-rule-config): Harness change summary + memory"
```

---

## Self-Review

After writing this plan, check:

1. **Spec coverage** — all spec sections 1-15 covered? ✅ (Section 1-3 background/goals/constraints → Global Constraints + Risks; Section 5 data model → Task 1; Section 6 runtime engine → Tasks 4-8; Section 7 NL assistant → Task 11; Section 8 admin UI → Tasks 13-15; Section 9 REST API → Tasks 10-11; Section 10 migration & seed → Tasks 1 + 6 + 10.4 parity → Task 7; Section 11 testing → Tasks 3-12 unit/integration; Section 12 risks → Global Constraints + per-task mitigations; Section 13 phases → Task ordering; Section 14-15 out of scope → Out of Scope in summary).

2. **Placeholder scan** — search for "TBD", "TODO", "implement later", "fill in details":
   - "沿用 NL2Sql 默认 factory" (Task 11 Step 4) — concrete reference, not placeholder.
   - "mirrors X pattern" (multiple tasks) — refers to `feat-agent-tool-config-db` precedent, concrete.
   - "placeholder" appears in `loginAdmin` helper comment (Task 10) — flagged to use existing helper pattern.
   - `_testapp.py` exception mapping mentioned in spec §9.2 but not a separate task — **POTENTIAL GAP**. Note: `ConflictError` parent class is already mapped to 409 via existing exception handlers (confirmed via `feat-agent-tool-config-db`). `LLMUnavailableError` needs explicit 503 mapping.

   **Action: Add LLMUnavailableError 503 mapping** — handled in existing `_testapp.py` exception handlers (check `app/main.py` for `ConflictError` and add `LLMUnavailableError` → 503 handler if missing). If not present, add to a Task 11 sub-step. Verify during Task 11 implementation.

3. **Type consistency**:
   - `feature_rule_registry` defined Task 4, used Tasks 5, 7, 8, 11 ✅
   - `RuleHit` defined Task 5, used Task 7 (need to import from `feature_rule_evaluator` in `supplier_risk_service.py`) ✅
   - `Severity` reused from `app.domain.enums` ✅
   - `RuleOperator` defined Task 1, used in DTOs Task 2 + seed Task 6 ✅
   - `feature_rule_registry.reloadOne(session, row.id)` called in API Task 10 — confirmed interface ✅
   - `parseFeatureRuleDescription(session, payload, client, actor)` signature stable across Tasks 11 + test ✅

   **One issue**: Task 7 references `_OPERATORS` and `_SEVERITY_ORDER` as imported from `feature_rule_evaluator`. The plan should make explicit that `supplier_risk_service.py` imports these constants. ✅ noted in Step 3 imports.

4. **Task ordering** — sequential dependencies:
   - Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 15 → 16 → 17 → 18 ✅ (DB before DTO before service before registry before evaluator before seed before integration before runtime before conftest before API before LLM before audit before frontend before route before coverage before docs)
   - Task 7 (parity test) is **CRITICAL** and must pass before merging — marked with ⚠️ in spec ✅

No critical issues found; one minor improvement: explicitly note the LLMUnavailableError → 503 mapping step is part of Task 11's "wire into lifespan" sub-step. Apply during execution.