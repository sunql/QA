# 多步问答「落库 + 自动重试 + 动态压缩 + 手动续跑」实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让多步 NL2SQL 的每一步执行结果（SQL / data / 错误 / token）实时落库，使网络类失败可自动重试、永久类失败可手动续跑、上下文临界可动态压缩。

**Architecture:** 新增两张 UUID 主键表（`multi_step_run` / `multi_step_step`）承载 run 与 step 状态机；新增三个纯函数模块（`multi_step_compressor` / `multi_step_retry` / `multi_step_persistence`）承担压缩、错误分类、落库；通过一个新的 mixin（`MultiStepPersistMixin`）把钩子挂到现有 `_executeDataStep` 调用点，避免继续膨胀 `chat_multistep.py`；续跑走新增 SSE 端点 `POST /api/v1/chat/multi-step/{runId}/resume`。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.x（async, `Mapped` 声明式）/ Pydantic v2 / Alembic / pytest + pytest-asyncio / PostgreSQL 16 / React + TypeScript（前端）

**Spec:** `docs/superpowers/specs/2026-10-05-multi-step-persist.md`

## Global Constraints

以下约束对**每一个** task 生效，不再逐条重复：

- **测试必须用真实 PostgreSQL**，禁止 sqlite 内存库、禁止直接调 service 跳过 API 链路（`Harness/rules/测试规范.md`）。运行前必须 export：
  - `TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test`
  - `TEST_NEO4J_URI=bolt://localhost:7688`（两者缺一会 fail-fast）
- **绝对禁止连 5433 端口**（那是生产库 `qa-postgres`）。测试库是 5434 的 `qa_metadata_test`。
- **禁止直接执行 `alembic upgrade head`** —— 项目 alembic 默认指向生产库。迁移脚本写完只做静态审查 + 在测试库显式指定 URL 应用。
- **不可变数据**：新建对象、返回新副本，禁止原地修改已有对象（冻结 dataclass 用 `dataclasses.replace`）。
- **文件 200-400 行为宜，硬上限 800 行**；函数 < 50 行；嵌套 ≤ 4 层。
- **命名**：变量/函数 `camelCase`；类型/组件 `PascalCase`；常量 `UPPER_SNAKE_CASE`；**ORM / Pydantic 字段 `snake_case`**（与 DB 列及 JSON 契约一致，刻意偏离 PEP 8）。
- **禁止后端覆盖用户选择的 `modelId`**。任何「按问题内容自动改派模型」的逻辑一律不得出现（见 memory `qa-system-no-model-override`）。
- **每次 LLM 调用必须记录 token 与成本**（`_recordUsage`），重试路径也不得漏计。
- `chat_multistep.py` 当前 827 行，用户豁免到 1000 行；本计划**不得**让它超过 1000 行 —— 新逻辑放独立文件。
- 提交信息格式：`<type>: <description>`（type ∈ feat, fix, refactor, docs, test, chore, perf, ci）。禁止 `git add -A`（会扫入无关未跟踪文件）。

---

### Task 1: 领域模型 + 迁移 0114

**Files:**
- Create: `backend/app/domain/multi_step_models.py`
- Create: `backend/alembic/versions/0114_multi_step_persist.py`
- Test: `backend/app/tests/integration/test_multi_step_persist_models.py`

**Interfaces:**
- Consumes: `Base`（来自 `app.domain.models`，与 `research_models.py` 同源）、`research_session.id`（0111 已建，UUID PK）
- Produces:
  - `MultiStepRun`（字段 `id, session_id, question, model_id, status, total_steps, completed_steps, current_step_idx, compressed_count, resume_count, version, idempotency_keys, error_summary, started_at, updated_at, finished_at`）
  - `MultiStepStep`（字段 `id, run_id, step_index, status, sub_question, sql, sql_hash, data, data_compressed, chart_option, model_used, tokens_used, cost, attempt_count, last_error, last_error_kind, started_at, updated_at, finished_at`）
  - 常量 `RUN_STATUS_RUNNING/SUCCEEDED/FAILED/PARTIALLY_FAILED`、`STEP_STATUS_PENDING/RUNNING/SUCCEEDED/FAILED/SKIPPED/COMPRESSED`

- [ ] **Step 1: 确认 `Base` 的导入位置与 UUID 惯例**

Run:
```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
grep -n "^from\|^import" app/domain/research_models.py | head -20
grep -rn "class Base" app/domain/*.py
```
Expected: 看到 `research_models.py` 从哪里 import `Base`，以及 `_utcnow` 的写法。**按它的原样照抄**，不要另起一套。

- [ ] **Step 2: 写失败测试**

`backend/app/tests/integration/test_multi_step_persist_models.py`:
```python
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.domain.multi_step_models import (
    STEP_STATUS_SUCCEEDED,
    MultiStepRun,
    MultiStepStep,
)


@pytest.mark.asyncio
async def testRunAndStepRoundTrip(db_session):
    # Arrange：先建一条 research_session 满足外键
    from app.domain.research_models import ResearchSession

    session_row = ResearchSession(id=uuid.uuid4(), title="msp-test", created_by=1)
    db_session.add(session_row)
    await db_session.flush()

    run = MultiStepRun(
        id=uuid.uuid4(),
        session_id=session_row.id,
        question="第一步查A，第二步查B",
        model_id=3,
        total_steps=2,
    )
    db_session.add(run)
    await db_session.flush()

    step = MultiStepStep(
        id=uuid.uuid4(),
        run_id=run.id,
        step_index=0,
        status=STEP_STATUS_SUCCEEDED,
        sub_question="查A",
        sql="SELECT 1 FROM dual",
        data=[{"a": 1}],
        tokens_used=15,
    )
    db_session.add(step)
    await db_session.commit()

    # Act
    loaded = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.id == run.id))
    ).scalar_one()

    # Assert
    assert loaded.status == "running"
    assert loaded.resume_count == 0
    assert loaded.version == 0
    assert loaded.idempotency_keys == []
    assert isinstance(loaded.started_at, datetime)

    steps = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == run.id)
        )
    ).scalars().all()
    assert len(steps) == 1
    assert steps[0].data == [{"a": 1}]
    assert steps[0].data_compressed is None
    assert steps[0].cost is None or steps[0].cost == 0


@pytest.mark.asyncio
async def testDuplicateStepIndexRejected(db_session):
    from app.domain.research_models import ResearchSession

    session_row = ResearchSession(id=uuid.uuid4(), title="msp-dup", created_by=1)
    db_session.add(session_row)
    await db_session.flush()
    run = MultiStepRun(
        id=uuid.uuid4(), session_id=session_row.id, question="q", model_id=None, total_steps=1
    )
    db_session.add(run)
    await db_session.flush()

    db_session.add_all([
        MultiStepStep(id=uuid.uuid4(), run_id=run.id, step_index=0, status="pending", sub_question="a"),
        MultiStepStep(id=uuid.uuid4(), run_id=run.id, step_index=0, status="pending", sub_question="b"),
    ])
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()
```

- [ ] **Step 3: 先跑迁移，再跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
export TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test
export TEST_NEO4J_URI=bolt://localhost:7688
pytest app/tests/integration/test_multi_step_persist_models.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.domain.multi_step_models'`

- [ ] **Step 4: 写模型**

`backend/app/domain/multi_step_models.py`:
```python
"""多步问答落库模型（spec: docs/superpowers/specs/2026-10-05-multi-step-persist.md §3）。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base

RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_PARTIALLY_FAILED = "partially_failed"

STEP_STATUS_PENDING = "pending"
STEP_STATUS_RUNNING = "running"
STEP_STATUS_SUCCEEDED = "succeeded"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_SKIPPED = "skipped"
STEP_STATUS_COMPRESSED = "compressed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MultiStepRun(Base):
    __tablename__ = "multi_step_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=RUN_STATUS_RUNNING)
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_step_idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    compressed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resume_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_multi_step_run_session_updated", "session_id", "updated_at"),
        Index("ix_multi_step_run_status_updated", "status", "updated_at"),
    )


class MultiStepStep(Base):
    __tablename__ = "multi_step_step"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("multi_step_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STEP_STATUS_PENDING)
    sub_question: Mapped[str] = mapped_column(Text, nullable=False)
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    data_compressed: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    chart_option: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "step_index", name="uq_multi_step_step_run_index"),
        Index("ix_multi_step_step_status_updated", "status", "updated_at"),
    )
```

若 Step 1 显示 `Base` 来自别处，改 import 行即可；字段定义不变。

- [ ] **Step 5: 写迁移 0114**

`backend/alembic/versions/0114_multi_step_persist.py`:
```python
"""多步问答落库：multi_step_run + multi_step_step

触发：spec docs/superpowers/specs/2026-10-05-multi-step-persist.md §3/§10.1
变更：新建两张表（UUID PK，FK CASCADE 到 research_session / multi_step_run）
幂等性：op.create_table 前不判存在，重复执行会报错；本迁移只跑一次
两库同步：需在 qa_metadata(prod) 与 qa_metadata_test 分别应用
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0114"
down_revision: str | None = "0113"
branch_labels = None
depends_on = None

_JSONB_EMPTY_LIST = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.create_table(
        "multi_step_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("total_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_step_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compressed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_keys", postgresql.JSONB(), nullable=False, server_default=_JSONB_EMPTY_LIST),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_multi_step_run_session_id", "multi_step_run", ["session_id"])
    op.create_index("ix_multi_step_run_session_updated", "multi_step_run", ["session_id", "updated_at"])
    op.create_index("ix_multi_step_run_status_updated", "multi_step_run", ["status", "updated_at"])

    op.create_table(
        "multi_step_step",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("multi_step_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("sub_question", sa.Text(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=True),
        sa.Column("sql_hash", sa.String(64), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("data_compressed", postgresql.JSONB(), nullable=True),
        sa.Column("chart_option", postgresql.JSONB(), nullable=True),
        sa.Column("model_used", sa.String(64), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_kind", sa.String(20), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", "step_index", name="uq_multi_step_step_run_index"),
    )
    op.create_index("ix_multi_step_step_run_id", "multi_step_step", ["run_id"])
    op.create_index("ix_multi_step_step_status_updated", "multi_step_step", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_multi_step_step_status_updated", table_name="multi_step_step")
    op.drop_index("ix_multi_step_step_run_id", table_name="multi_step_step")
    op.drop_table("multi_step_step")
    op.drop_index("ix_multi_step_run_status_updated", table_name="multi_step_run")
    op.drop_index("ix_multi_step_run_session_updated", table_name="multi_step_run")
    op.drop_index("ix_multi_step_run_session_id", table_name="multi_step_run")
    op.drop_table("multi_step_run")
```

- [ ] **Step 6: 在测试库应用迁移**

**不要**用裸 `alembic upgrade head`。显式指定测试库 URL：
```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
alembic -x db_url=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test upgrade 0114
```
若项目的 `env.py` 不支持 `-x db_url`，改用：
```bash
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test alembic upgrade 0114
```
先 `grep -n "db_url\|DATABASE_URL\|getSettings" alembic/env.py` 确认用哪个。**执行前再确认一次 URL 里是 5434 不是 5433。**

- [ ] **Step 7: 跑测试确认通过**

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test
export TEST_NEO4J_URI=bolt://localhost:7688
pytest app/tests/integration/test_multi_step_persist_models.py -v
```
Expected: 2 passed

- [ ] **Step 8: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/domain/multi_step_models.py \
        backend/alembic/versions/0114_multi_step_persist.py \
        backend/app/tests/integration/test_multi_step_persist_models.py
git commit -m "feat(multi-step): 新增 multi_step_run/multi_step_step 模型与 0114 迁移"
```

---

### Task 2: 落库仓储 `MultiStepRepository`

**Files:**
- Create: `backend/app/services/multi_step_persistence.py`
- Test: `backend/app/tests/integration/test_multi_step_persist_repo.py`

**Interfaces:**
- Consumes: Task 1 的 `MultiStepRun` / `MultiStepStep` + 状态常量
- Produces（全部 `async`，第一个参数 `session: AsyncSession`）：
  - `createRun(session, *, sessionId: uuid.UUID, question: str, modelId: int | None, totalSteps: int) -> MultiStepRun`
  - `createSteps(session, *, runId: uuid.UUID, subQuestions: list[str]) -> list[MultiStepStep]`
  - `markStepRunning(session, step: MultiStepStep) -> None`
  - `finishStep(session, step, *, status: str, sql: str | None = None, data: list | None = None, chartOption: dict | None = None, modelUsed: str | None = None, tokens: int = 0, cost: float = 0) -> None`
  - `recordStepError(session, step, *, message: str, kind: str) -> None`
  - `updateRun(session, run, *, status: str | None = None, completedSteps: int | None = None, currentStepIdx: int | None = None, compressedCount: int | None = None, errorSummary: str | None = None, finished: bool = False) -> None`
  - `loadRun(session, runId: uuid.UUID) -> MultiStepRun | None`
  - `loadSteps(session, runId: uuid.UUID) -> list[MultiStepStep]`（按 `step_index` 升序）
  - `resetStepsFrom(session, *, runId: uuid.UUID, fromStepIndex: int) -> int`
  - `appendIdempotencyKey(session, run) -> None`（仅当 key 未在列表中时追加）

- [ ] **Step 1: 写失败测试**

`backend/app/tests/integration/test_multi_step_persist_repo.py`:
```python
import uuid

import pytest

from app.services import multi_step_persistence as repo


@pytest.fixture
async def sessionRow(db_session):
    from app.domain.research_models import ResearchSession

    row = ResearchSession(id=uuid.uuid4(), title="repo-test", created_by=1)
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def testCreateRunAndSteps(db_session, sessionRow):
    # Act
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="两步题", modelId=3, totalSteps=2
    )
    steps = await repo.createSteps(
        db_session, runId=run.id, subQuestions=["查A", "查B"]
    )
    await db_session.commit()

    # Assert
    assert run.status == "running"
    assert run.total_steps == 2
    assert [s.step_index for s in steps] == [0, 1]
    assert all(s.status == "pending" for s in steps)
    assert [s.sub_question for s in steps] == ["查A", "查B"]


@pytest.mark.asyncio
async def testFinishStepWritesDataAndUsage(db_session, sessionRow):
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="q", modelId=3, totalSteps=1
    )
    (step,) = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A"])
    await repo.markStepRunning(db_session, step)

    await repo.finishStep(
        db_session, step, status="succeeded",
        sql="SELECT 1", data=[{"n": 1}], modelUsed="qwen", tokens=15, cost=0.0001,
    )
    await db_session.commit()

    loaded = (await repo.loadSteps(db_session, run.id))[0]
    assert loaded.status == "succeeded"
    assert loaded.data == [{"n": 1}]
    assert loaded.tokens_used == 15
    assert loaded.attempt_count == 0


@pytest.mark.asyncio
async def testRecordStepErrorAccumulatesAttempts(db_session, sessionRow):
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="q", modelId=3, totalSteps=1
    )
    (step,) = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A"])
    await repo.recordStepError(db_session, step, message="timeout", kind="transient")
    await repo.recordStepError(db_session, step, message="timeout again", kind="transient")
    await db_session.commit()

    loaded = (await repo.loadSteps(db_session, run.id))[0]
    assert loaded.attempt_count == 2
    assert loaded.last_error == "timeout again"
    assert loaded.last_error_kind == "transient"
    assert loaded.status == "running"  # 未终态


@pytest.mark.asyncio
async def testUpdateRunClosesRun(db_session, sessionRow):
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="q", modelId=3, totalSteps=2
    )
    await repo.updateRun(
        db_session, run, status="partially_failed", completedSteps=1,
        currentStepIdx=1, errorSummary="第 2 步跳过", finished=True,
    )
    await db_session.commit()

    loaded = await repo.loadRun(db_session, run.id)
    assert loaded.status == "partially_failed"
    assert loaded.completed_steps == 1
    assert loaded.finished_at is not None


@pytest.mark.asyncio
async def testResetStepsFromClearsErrorsAndKeepsSucceeded(db_session, sessionRow):
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="q", modelId=3, totalSteps=3
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["a", "b", "c"])
    await repo.finishStep(db_session, steps[0], status="succeeded", data=[{"x": 1}], sql="SELECT 1")
    await repo.finishStep(db_session, steps[1], status="failed")
    await repo.recordStepError(db_session, steps[1], message="boom", kind="permanent")

    await repo.resetStepsFrom(db_session, runId=run.id, fromStepIndex=1)
    await db_session.commit()

    loaded = await repo.loadSteps(db_session, run.id)
    assert loaded[0].status == "succeeded" and loaded[0].data == [{"x": 1}]
    assert loaded[1].status == "pending" and loaded[1].last_error is None
    assert loaded[2].status == "pending"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
export TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test
export TEST_NEO4J_URI=bolt://localhost:7688
pytest app/tests/integration/test_multi_step_persist_repo.py -v
```
Expected: FAIL — `ImportError: cannot import name 'multi_step_persistence'`

- [ ] **Step 3: 写实现**

`backend/app/services/multi_step_persistence.py`:
```python
"""多步 run/step 的落库与查询（spec §3/§4）。纯数据访问，无 LLM 调用。"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.multi_step_models import (
    RUN_STATUS_RUNNING,
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    MultiStepRun,
    MultiStepStep,
    _utcnow,
)


async def createRun(
    session: AsyncSession,
    *,
    sessionId: uuid.UUID,
    question: str,
    modelId: int | None,
    totalSteps: int,
) -> MultiStepRun:
    if totalSteps < 0:
        raise ValueError(f"totalSteps must be >= 0, got {totalSteps}")
    run = MultiStepRun(
        id=uuid.uuid4(),
        session_id=sessionId,
        question=question,
        model_id=modelId,
        status=RUN_STATUS_RUNNING,
        total_steps=totalSteps,
    )
    session.add(run)
    await session.flush()
    return run


async def createSteps(
    session: AsyncSession, *, runId: uuid.UUID, subQuestions: list[str]
) -> list[MultiStepStep]:
    steps = [
        MultiStepStep(
            id=uuid.uuid4(),
            run_id=runId,
            step_index=index,
            status=STEP_STATUS_PENDING,
            sub_question=text,
        )
        for index, text in enumerate(subQuestions)
    ]
    session.add_all(steps)
    await session.flush()
    return steps


async def markStepRunning(session: AsyncSession, step: MultiStepStep) -> None:
    step.status = STEP_STATUS_RUNNING
    step.started_at = _utcnow()
    await session.flush()


async def finishStep(
    session: AsyncSession,
    step: MultiStepStep,
    *,
    status: str,
    sql: str | None = None,
    data: list | None = None,
    chartOption: dict | None = None,
    modelUsed: str | None = None,
    tokens: int = 0,
    cost: float = 0,
) -> None:
    step.status = status
    if sql is not None:
        step.sql = sql
        step.sql_hash = _sqlHash(sql)
    if data is not None:
        step.data = data
    if chartOption is not None:
        step.chart_option = chartOption
    if modelUsed is not None:
        step.model_used = modelUsed
    step.tokens_used = (step.tokens_used or 0) + tokens
    step.cost = Decimal(str(step.cost or 0)) + Decimal(str(cost))
    step.finished_at = _utcnow()
    await session.flush()


async def recordStepError(
    session: AsyncSession, step: MultiStepStep, *, message: str, kind: str
) -> None:
    step.attempt_count = (step.attempt_count or 0) + 1
    step.last_error = message[:2000]
    step.last_error_kind = kind
    step.status = STEP_STATUS_RUNNING
    await session.flush()


async def updateRun(
    session: AsyncSession,
    run: MultiStepRun,
    *,
    status: str | None = None,
    completedSteps: int | None = None,
    currentStepIdx: int | None = None,
    compressedCount: int | None = None,
    errorSummary: str | None = None,
    finished: bool = False,
) -> None:
    if status is not None:
        run.status = status
    if completedSteps is not None:
        run.completed_steps = completedSteps
    if currentStepIdx is not None:
        run.current_step_idx = currentStepIdx
    if compressedCount is not None:
        run.compressed_count = compressedCount
    if errorSummary is not None:
        run.error_summary = errorSummary[:2000]
    if finished:
        run.finished_at = _utcnow()
    await session.flush()


async def loadRun(session: AsyncSession, runId: uuid.UUID) -> MultiStepRun | None:
    return (
        await session.execute(select(MultiStepRun).where(MultiStepRun.id == runId))
    ).scalar_one_or_none()


async def loadSteps(session: AsyncSession, runId: uuid.UUID) -> list[MultiStepStep]:
    rows = (
        await session.execute(
            select(MultiStepStep)
            .where(MultiStepStep.run_id == runId)
            .order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    return list(rows)


async def resetStepsFrom(
    session: AsyncSession, *, runId: uuid.UUID, fromStepIndex: int
) -> int:
    """把 >= fromStepIndex 的步重置为 pending 并清空错误；返回受影响行数。"""
    steps = await loadSteps(session, runId)
    touched = 0
    for step in steps:
        if step.step_index < fromStepIndex:
            continue
        step.status = STEP_STATUS_PENDING
        step.last_error = None
        step.last_error_kind = None
        step.attempt_count = 0
        step.finished_at = None
        touched += 1
    await session.flush()
    return touched


async def appendIdempotencyKey(session: AsyncSession, run: MultiStepRun, key: str) -> None:
    existing = list(run.idempotency_keys or [])
    if key in existing:
        return
    run.idempotency_keys = existing + [key]
    await session.flush()


def _sqlHash(sql: str) -> str:
    import hashlib

    return hashlib.sha256(sql.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test
export TEST_NEO4J_URI=bolt://localhost:7688
pytest app/tests/integration/test_multi_step_persist_repo.py -v
```
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/multi_step_persistence.py \
        backend/app/tests/integration/test_multi_step_persist_repo.py
git commit -m "feat(multi-step): 新增 MultiStepRepository 落库与查询"
```

---

### Task 3: 错误分类与瞬态重试

**Files:**
- Create: `backend/app/services/multi_step_retry.py`
- Test: `backend/app/tests/unit/test_multi_step_retry.py`

**Interfaces:**
- Consumes: `app.domain.exceptions.LLMUnavailableError`、`app.services.llm_retry_policy.isRetryableLlmError`（参考用）
- Produces:
  - 常量 `TRANSIENT_WAITS = (1, 2, 4)`、`MAX_ATTEMPTS = 3`、`ERROR_KIND_TRANSIENT = "transient"`、`ERROR_KIND_PERMANENT = "permanent"`
  - `classifyStepError(exc: BaseException) -> str`
  - `async def runWithTransientRetry(call, *, sleep=asyncio.sleep, onError=None) -> tuple[Any, int]`

**设计说明（与既有策略的差异，必须写进 docstring）:** `llm_retry_policy.isRetryableLlmError` 对 `Nl2SqlError` 一律返回可重试；但多步场景下 plan 校验失败属于**永久**错误（重试无意义、白烧 token）。故本模块自带分类器，不复用那一条规则。

- [ ] **Step 1: 写失败测试**

`backend/app/tests/unit/test_multi_step_retry.py`:
```python
import asyncio

import httpx
import pytest

from app.domain.exceptions import LLMUnavailableError, Nl2SqlError
from app.services.multi_step_retry import (
    ERROR_KIND_PERMANENT,
    ERROR_KIND_TRANSIENT,
    MAX_ATTEMPTS,
    classifyStepError,
    runWithTransientRetry,
)


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectError("refused"), ERROR_KIND_TRANSIENT),
        (httpx.ReadTimeout("slow"), ERROR_KIND_TRANSIENT),
        (asyncio.TimeoutError(), ERROR_KIND_TRANSIENT),
        (LLMUnavailableError("no client"), ERROR_KIND_TRANSIENT),
        (_StatusError(429), ERROR_KIND_TRANSIENT),
        (_StatusError(502), ERROR_KIND_TRANSIENT),
        (_StatusError(503), ERROR_KIND_TRANSIENT),
        (_StatusError(400), ERROR_KIND_PERMANENT),
        (Nl2SqlError("plan 校验失败"), ERROR_KIND_PERMANENT),
        (ValueError("bad input"), ERROR_KIND_PERMANENT),
    ],
)
def testClassifyStepError(exc, expected):
    assert classifyStepError(exc) == expected


@pytest.mark.asyncio
async def testRetrySucceedsOnSecondAttempt():
    # Arrange
    waits: list[float] = []

    async def fakeSleep(seconds: float) -> None:
        waits.append(seconds)

    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("refused")
        return "ok"

    # Act
    result, attempts = await runWithTransientRetry(call, sleep=fakeSleep)

    # Assert
    assert result == "ok"
    assert attempts == 2
    assert waits == [1]


@pytest.mark.asyncio
async def testPermanentErrorDoesNotRetry():
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        raise Nl2SqlError("permanent")

    with pytest.raises(Nl2SqlError):
        await runWithTransientRetry(call, sleep=lambda s: asyncio.sleep(0))
    assert calls["n"] == 1


@pytest.mark.asyncio
async def testTransientErrorExhaustsAttemptsThenRaises():
    waits: list[float] = []

    async def fakeSleep(seconds: float) -> None:
        waits.append(seconds)

    async def call():
        raise httpx.ConnectError("always down")

    with pytest.raises(httpx.ConnectError):
        await runWithTransientRetry(call, sleep=fakeSleep)
    assert waits == [1, 2]  # 3 次尝试之间只等 2 次


@pytest.mark.asyncio
async def testOnErrorHookSeesEachTransientFailure():
    seen: list[str] = []

    async def call():
        raise httpx.ConnectError("down")

    async def onError(exc: Exception, attempt: int) -> None:
        seen.append(f"{type(exc).__name__}:{attempt}")

    with pytest.raises(httpx.ConnectError):
        await runWithTransientRetry(call, sleep=lambda s: asyncio.sleep(0), onError=onError)
    assert seen == ["ConnectError:1", "ConnectError:2", "ConnectError:3"]


def testMaxAttemptsMatchesWaitTable():
    assert MAX_ATTEMPTS == 3
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_multi_step_retry.py -v
```
Expected: FAIL — `ModuleNotFoundError: app.services.multi_step_retry`

- [ ] **Step 3: 写实现**

`backend/app/services/multi_step_retry.py`:
```python
"""多步执行的服务端错误分类与瞬态重试（spec §6）。

与 app.services.llm_retry_policy 的差异：那边把 Nl2SqlError 一律视为可重试，
多步场景下 plan/SQL 校验失败重试无意义且白烧 token，故这里独立分类。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from app.domain.exceptions import LLMUnavailableError, Nl2SqlError

logger = logging.getLogger(__name__)

ERROR_KIND_TRANSIENT = "transient"
ERROR_KIND_PERMANENT = "permanent"

#: 每次瞬态失败后的等待秒数；长度即「重试次数上限 - 1」
TRANSIENT_WAITS: tuple[int, ...] = (1, 2, 4)
MAX_ATTEMPTS: int = len(TRANSIENT_WAITS)

_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

T = TypeVar("T")


def classifyStepError(exc: BaseException) -> str:
    """把异常分成 transient（可自动重试）或 permanent（转人工）。"""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException)):
        return ERROR_KIND_TRANSIENT
    if isinstance(exc, asyncio.TimeoutError):
        return ERROR_KIND_TRANSIENT
    if isinstance(exc, LLMUnavailableError):
        return ERROR_KIND_TRANSIENT
    if isinstance(exc, Nl2SqlError):
        return ERROR_KIND_PERMANENT

    status = _statusCode(exc)
    if status is not None:
        return ERROR_KIND_TRANSIENT if status in _TRANSIENT_STATUS else ERROR_KIND_PERMANENT
    return ERROR_KIND_PERMANENT


def _statusCode(exc: BaseException) -> int | None:
    for candidate in (exc, getattr(exc, "__cause__", None)):
        code = getattr(candidate, "status_code", None)
        if isinstance(code, int):
            return code
    return None


async def runWithTransientRetry(
    call: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    onError: Callable[[Exception, int], Awaitable[None]] | None = None,
) -> tuple[T, int]:
    """执行 call，瞬态错误按 TRANSIENT_WAITS 退避重试。

    返回 (结果, 实际尝试次数)。永久错误立即抛出；瞬态错误耗尽后抛出最后一次异常。

    只捕获 Exception：CancelledError 继承自 BaseException，必须让它透传，
    否则客户端断连时重试会把取消信号吞掉。
    """
    lastError: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await call(), attempt
        except Exception as exc:  # noqa: BLE001 - 需按分类决定是否重试
            kind = classifyStepError(exc)
            if onError is not None:
                await onError(exc, attempt)
            if kind != ERROR_KIND_TRANSIENT:
                raise
            lastError = exc
            if attempt <= len(TRANSIENT_WAITS):
                logger.warning("multi-step 第 %d 次尝试瞬态失败，%.0fs 后重试：%s",
                               attempt, TRANSIENT_WAITS[attempt - 1], exc)
                await sleep(TRANSIENT_WAITS[attempt - 1])
    assert lastError is not None
    raise lastError
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest app/tests/unit/test_multi_step_retry.py -v
```
Expected: 15 passed（`testClassifyStepError` 10 个参数化用例 + 5 个测试函数）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/multi_step_retry.py backend/app/tests/unit/test_multi_step_retry.py
git commit -m "feat(multi-step): 新增错误分类与瞬态重试（1s/2s/4s，3 次封顶）"
```

---

### Task 4: 上下文压缩与 token 估算

**Files:**
- Create: `backend/app/services/multi_step_compressor.py`
- Test: `backend/app/tests/unit/test_multi_step_compressor.py`

**Interfaces:**
- Produces:
  - 常量 `COMPRESS_THRESHOLD = 0.7`、`DEFAULT_MAX_ROWS = 30`、`MAX_DISTINCT_VALUES = 50`、`TOP_N_EXTREMES = 5`
  - `classifyColumn(name: str, values: list) -> str`（返回 `"time"` / `"numeric"` / `"category"`）
  - `compressStepData(rows: list[dict], *, maxRows: int = DEFAULT_MAX_ROWS) -> dict`
  - `estimatePromptTokens(text: str) -> int`
  - `shouldCompress(estimatedTokens: int, maxInputTokens: int, *, threshold: float = COMPRESS_THRESHOLD) -> bool`

- [ ] **Step 1: 写失败测试**

`backend/app/tests/unit/test_multi_step_compressor.py`:
```python
import pytest

from app.services.multi_step_compressor import (
    COMPRESS_THRESHOLD,
    MAX_DISTINCT_VALUES,
    TOP_N_EXTREMES,
    classifyColumn,
    compressStepData,
    estimatePromptTokens,
    shouldCompress,
)


def testClassifyColumnBySuffixAndType():
    assert classifyColumn("stat_month", ["2025-01", "2025-02"]) == "time"
    assert classifyColumn("日期", ["2025-01-01"]) == "time"
    assert classifyColumn("amount", [1, 2, 3]) == "numeric"
    assert classifyColumn("qty", [1.5, None, 2.5]) == "numeric"
    assert classifyColumn("supplier_name", ["A", "B"]) == "category"
    # 数字看起来像字符串时按类别处理
    assert classifyColumn("code", ["001", "002"]) == "category"


def testCompressKeepsRowsUnderLimitAndAggregatesNumerics():
    rows = [{"stat_month": f"2025-{m:02d}", "amount": m * 10} for m in range(1, 13)]

    result = compressStepData(rows, maxRows=5)

    assert len(result["rows"]) == 5
    assert result["meta"]["original_rows"] == 12
    assert result["meta"]["compressed_rows"] == 5
    amountSummary = result["columns"]["amount"]
    assert amountSummary["max"] == 120
    assert amountSummary["min"] == 10
    assert amountSummary["sum"] == 780
    assert len(amountSummary["top"]) == TOP_N_EXTREMES
    assert amountSummary["top"][0]["amount"] == 120


def testCompressKeepsAllDistinctTimeValues():
    rows = [{"stat_month": f"2025-{m:02d}", "amount": m} for m in range(1, 13)]
    result = compressStepData(rows, maxRows=3)
    assert result["columns"]["stat_month"]["distinct"] == [f"2025-{m:02d}" for m in range(1, 13)]


def testCompressCapsCategoryDistinctValues():
    rows = [{"name": f"n{i}", "amount": i} for i in range(200)]
    result = compressStepData(rows, maxRows=1)
    assert len(result["columns"]["name"]["distinct"]) == MAX_DISTINCT_VALUES


def testCompressEmptyRowsIsSafe():
    result = compressStepData([], maxRows=10)
    assert result["rows"] == []
    assert result["meta"]["original_rows"] == 0
    assert result["meta"]["ratio"] == 1.0


def testCompressIgnoresBooleansAsNumeric():
    rows = [{"is_active": True, "amount": 1}, {"is_active": False, "amount": 2}]
    result = compressStepData(rows, maxRows=2)
    assert "max" not in result["columns"]["is_active"]


def testEstimatePromptTokensCountsCjkAndLatin():
    assert estimatePromptTokens("") == 0
    # 4 个汉字 ≈ 4 token；8 个 latin 字符 ≈ 2 token
    assert estimatePromptTokens("供应商名称") == 5
    assert estimatePromptTokens("abcdefgh") == 2


@pytest.mark.parametrize(
    "estimated, maxInput, expected",
    [
        (700, 1000, False),   # 恰好 70%，不压
        (701, 1000, True),
        (100, 1000, False),
        (950, 1000, True),
    ],
)
def testShouldCompress(estimated, maxInput, expected):
    assert shouldCompress(estimated, maxInput) is expected
    assert COMPRESS_THRESHOLD == 0.7
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest app/tests/unit/test_multi_step_compressor.py -v
```
Expected: FAIL — `ModuleNotFoundError: app.services.multi_step_compressor`

- [ ] **Step 3: 写实现**

`backend/app/services/multi_step_compressor.py`:
```python
"""多步上下文动态压缩（spec §5，方案③：行截断 + 关键列提取 + 极值点）。

零额外 LLM 调用；原始 data 永不删除，压缩结果另存 data_compressed。
"""
from __future__ import annotations

import re

COMPRESS_THRESHOLD = 0.7
DEFAULT_MAX_ROWS = 30
MAX_DISTINCT_VALUES = 50
TOP_N_EXTREMES = 5

_TIME_HINT = re.compile(
    r"(date|time|month|year|week|day|quarter|日期|时间|月份|年份|周|季度)", re.IGNORECASE
)

_KIND_TIME = "time"
_KIND_NUMERIC = "numeric"
_KIND_CATEGORY = "category"


def classifyColumn(name: str, values: list) -> str:
    if _TIME_HINT.search(name or ""):
        return _KIND_TIME
    present = [v for v in values if v is not None]
    if present and all(_isNumber(v) for v in present):
        return _KIND_NUMERIC
    return _KIND_CATEGORY


def compressStepData(rows: list[dict], *, maxRows: int = DEFAULT_MAX_ROWS) -> dict:
    if not rows:
        return {"rows": [], "columns": {}, "meta": {"original_rows": 0, "compressed_rows": 0, "ratio": 1.0}}

    columns = list(rows[0].keys())
    summary: dict[str, dict] = {}
    for column in columns:
        values = [row.get(column) for row in rows]
        summary[column] = _summarizeColumn(column, values, rows, columns)

    kept = rows[:maxRows]
    original = len(rows)
    return {
        "rows": kept,
        "columns": summary,
        "meta": {
            "original_rows": original,
            "compressed_rows": len(kept),
            "ratio": round(len(kept) / original, 4),
        },
    }


def estimatePromptTokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4


def shouldCompress(
    estimatedTokens: int, maxInputTokens: int, *, threshold: float = COMPRESS_THRESHOLD
) -> bool:
    if maxInputTokens <= 0:
        return False
    return estimatedTokens > maxInputTokens * threshold


def _summarizeColumn(
    column: str, values: list, rows: list[dict], columns: list[str]
) -> dict:
    present = [v for v in values if v is not None]
    kind = classifyColumn(column, values)

    if kind == _KIND_TIME:
        return {"distinct": _distinctSorted(present)}

    if kind == _KIND_NUMERIC:
        numbers = [float(v) for v in present if _isNumber(v)]
        if not numbers:
            return {"distinct": _distinctSorted(present)[:MAX_DISTINCT_VALUES]}
        summary = {
            "max": max(numbers),
            "min": min(numbers),
            "avg": round(sum(numbers) / len(numbers), 4),
            "sum": round(sum(numbers), 4),
        }
        ranked = sorted(
            (row for row in rows if _isNumber(row.get(column))),
            key=lambda row: abs(float(row[column])),
            reverse=True,
        )[:TOP_N_EXTREMES]
        summary["top"] = [{c: row.get(c) for c in columns} for row in ranked]
        return summary

    return {"distinct": _distinctSorted(present)[:MAX_DISTINCT_VALUES]}


def _distinctSorted(values: list) -> list:
    return sorted({v for v in values if v is not None}, key=lambda v: str(v))


def _isNumber(value: object) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest app/tests/unit/test_multi_step_compressor.py -v
```
Expected: 11 passed（7 个测试函数 + `testShouldCompress` 4 个参数化用例）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/multi_step_compressor.py backend/app/tests/unit/test_multi_step_compressor.py
git commit -m "feat(multi-step): 新增上下文压缩与 token 估算"
```

---

### Task 5: 钩子 mixin（落库 + 重试 + 压缩挂载点）

**Files:**
- Create: `backend/app/services/multi_step_persist_hooks.py`（目标 < 250 行）
- Modify: `backend/app/services/chat_service.py`（类基类列表加一个 mixin）
- Modify: `backend/app/config.py`（新增 `multiStepPersistEnabled` 静态开关）
- Test: `backend/app/tests/unit/test_multi_step_persist_hooks.py`

**Interfaces:**
- Consumes: Task 2 `multi_step_persistence`、Task 3 `runWithTransientRetry`/`classifyStepError`、Task 4 `compressStepData`/`shouldCompress`/`estimatePromptTokens`
- Produces: `class MultiStepPersistMixin`，方法（全部 `async`）：
  - `_isPersistEnabled(self, session) -> bool`
  - `_openRun(self, session, *, sessionId, question, modelId, subQuestions) -> MultiStepRun | None`
  - `_persistStepSuccess(self, session, step, *, status, sql, data, chartOption, modelUsed, tokens, cost) -> None`
  - `_persistStepFailure(self, session, step, exc) -> str`
  - `_closeRun(self, session, run, *, status, completedSteps, currentStepIdx, errorSummary) -> None`
  - `_maybeCompressPriorSteps(self, session, run, steps, *, nextStepIdx, maxInputTokens, injectionText) -> bool`

- [ ] **Step 1: 加静态开关**

`backend/app/config.py`，在 `rateLimitEnabled` 附近加一行：
```python
    multiStepPersistEnabled: bool = Field(default=True, alias="MULTI_STEP_PERSIST_ENABLED")
```

- [ ] **Step 2: 写失败测试**

`backend/app/tests/unit/test_multi_step_persist_hooks.py`:
```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.multi_step_persist_hooks import MultiStepPersistMixin


class _Host(MultiStepPersistMixin):
    pass


@pytest.mark.asyncio
async def testPersistDisabledSkipsEverything(monkeypatch):
    # Arrange
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.getSettings",
        lambda: SimpleNamespace(multiStepPersistEnabled=False),
    )
    host = _Host()
    session = AsyncMock()
    createRun = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.createRun", createRun
    )

    # Act
    run = await host._openRun(
        session, sessionId="s", question="q", modelId=1, subQuestions=["a"]
    )

    # Assert
    assert run is None
    createRun.assert_not_awaited()


@pytest.mark.asyncio
async def testPersistEnabledOpensRun(monkeypatch):
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.getSettings",
        lambda: SimpleNamespace(multiStepPersistEnabled=True),
    )
    host = _Host()
    session = AsyncMock()
    sentinel = object()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.createRun",
        AsyncMock(return_value=sentinel),
    )
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.createSteps",
        AsyncMock(return_value=[]),
    )

    run = await host._openRun(
        session, sessionId="s", question="q", modelId=1, subQuestions=["a"]
    )

    assert run is sentinel


@pytest.mark.asyncio
async def testPersistStepFailureRecordsClassification(monkeypatch):
    host = _Host()
    session = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.recordStepError", record
    )
    updateRun = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.updateRun", updateRun
    )

    kind = await host._persistStepFailure(
        session, SimpleNamespace(step_index=1), httpx.ConnectError("refused"), run=SimpleNamespace()
    )

    assert kind == "transient"
    assert record.await_args.kwargs["kind"] == "transient"
    assert updateRun.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def testMaybeCompressNoOpWhenUnderThreshold(monkeypatch):
    host = _Host()
    session = AsyncMock()
    finishStep = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.finishStep", finishStep
    )
    steps = [SimpleNamespace(step_index=0, status="succeeded", data=[{"a": 1}], data_compressed=None)]

    changed = await host._maybeCompressPriorSteps(
        session, SimpleNamespace(), steps,
        nextStepIdx=1, maxInputTokens=100000, injectionText="短文本",
    )

    assert changed is False
    finishStep.assert_not_awaited()
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_multi_step_persist_hooks.py -v
```
Expected: FAIL — `ModuleNotFoundError: app.services.multi_step_persist_hooks`

- [ ] **Step 4: 写 mixin**

`backend/app/services/multi_step_persist_hooks.py`:
```python
"""多步持久化的编排钩子（spec §3-§6）。

单独成模块的原因：chat_multistep.py 已达 827 行且被豁免到 1000 行，新逻辑不得再挤进去。
本 mixin 只做「调用仓储 + 决定要不要压缩/落库」，不含 NL2SQL 业务逻辑。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.multi_step_models import (
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIALLY_FAILED,
    RUN_STATUS_SUCCEEDED,
    MultiStepRun,
    MultiStepStep,
)
from app.services import multi_step_persistence as persistence
from app.services.multi_step_compressor import (
    estimatePromptTokens,
    shouldCompress,
)
from app.services.multi_step_compressor import compressStepData
from app.services.multi_step_retry import classifyStepError

logger = logging.getLogger(__name__)

_STEP_STATUS_COMPRESSED = "compressed"
_STEP_STATUS_SUCCEEDED = "succeeded"


class MultiStepPersistMixin:
    """给 ChatService 提供 run/step 落库、压缩判定、失败分类的钩子。"""

    async def _isPersistEnabled(self, session: AsyncSession) -> bool:
        return bool(getSettings().multiStepPersistEnabled)

    async def _openRun(
        self,
        session: AsyncSession,
        *,
        sessionId: Any,
        question: str,
        modelId: int | None,
        subQuestions: list[str],
    ) -> MultiStepRun | None:
        if not await self._isPersistEnabled(session):
            return None
        run = await persistence.createRun(
            session,
            sessionId=sessionId,
            question=question,
            modelId=modelId,
            totalSteps=len(subQuestions),
        )
        await persistence.createSteps(session, runId=run.id, subQuestions=subQuestions)
        return run

    async def _persistStepSuccess(
        self,
        session: AsyncSession,
        step: MultiStepStep,
        *,
        status: str = _STEP_STATUS_SUCCEEDED,
        sql: str | None = None,
        data: list | None = None,
        chartOption: dict | None = None,
        modelUsed: str | None = None,
        tokens: int = 0,
        cost: float = 0,
    ) -> None:
        if step is None:
            return
        await persistence.finishStep(
            session, step, status=status, sql=sql, data=data,
            chartOption=chartOption, modelUsed=modelUsed, tokens=tokens, cost=cost,
        )

    async def _persistStepFailure(
        self,
        session: AsyncSession,
        step: MultiStepStep,
        exc: Exception,
        *,
        run: MultiStepRun | None = None,
    ) -> str:
        kind = classifyStepError(exc)
        await persistence.recordStepError(
            session, step, message=f"{type(exc).__name__}: {exc}", kind=kind
        )
        if run is not None:
            await persistence.updateRun(
                session, run, status=RUN_STATUS_FAILED, currentStepIdx=step.step_index
            )
        return kind

    async def _closeRun(
        self,
        session: AsyncSession,
        run: MultiStepRun | None,
        *,
        status: str,
        completedSteps: int,
        currentStepIdx: int,
        errorSummary: str | None = None,
    ) -> None:
        if run is None:
            return
        await persistence.updateRun(
            session, run,
            status=status,
            completedSteps=completedSteps,
            currentStepIdx=currentStepIdx,
            errorSummary=errorSummary,
            finished=True,
        )

    async def _maybeCompressPriorSteps(
        self,
        session: AsyncSession,
        run: MultiStepRun | None,
        steps: list[MultiStepStep],
        *,
        nextStepIdx: int,
        maxInputTokens: int,
        injectionText: str,
    ) -> bool:
        """构造第 nextStepIdx 步 prompt 前调用。超阈值则压缩已成功步的 data。"""
        if run is None:
            return False
        estimated = estimatePromptTokens(injectionText)
        if not shouldCompress(estimated, maxInputTokens):
            return False

        compressedCount = 0
        for step in steps:
            if step.step_index >= nextStepIdx:
                break
            if step.status not in (_STEP_STATUS_SUCCEEDED, _STEP_STATUS_COMPRESSED):
                continue
            if step.data_compressed is not None:
                continue
            if not step.data:
                continue
            compressed = compressStepData(list(step.data))
            await persistence.finishStep(
                session, step, status=_STEP_STATUS_COMPRESSED
            )
            step.data_compressed = compressed
            compressedCount += 1

        if compressedCount:
            await persistence.updateRun(
                session, run,
                compressedCount=(run.compressed_count or 0) + compressedCount,
            )
            logger.info("多步压缩：run=%s 压缩 %d 步（估算 %d > %.0f%% of %d）",
                        run.id, compressedCount, estimated, 0.7 * 100, maxInputTokens)
        return compressedCount > 0


def runStatusFor(completed: int, total: int, anyFailed: bool, anySkipped: bool) -> str:
    """run 终态判定（spec §4.2）。"""
    if anyFailed and not anySkipped:
        return RUN_STATUS_FAILED
    if anySkipped:
        return RUN_STATUS_PARTIALLY_FAILED
    if completed >= total:
        return RUN_STATUS_SUCCEEDED
    return RUN_STATUS_FAILED
```

- [ ] **Step 5: 把 mixin 挂到 ChatService**

先在 `chat_service.py` 找到 `class ChatService(` 的基类列表：
```bash
grep -n "class ChatService" backend/app/services/chat_service.py
```
把 `MultiStepPersistMixin` 加进基类（顺序放在其他 mixin 之后）：
```python
from app.services.multi_step_persist_hooks import MultiStepPersistMixin


class ChatService(MultiStepMixin, MultiStepPersistMixin, ...其他原基类...):
```
**只加基类与 import，不动其他代码。**

- [ ] **Step 6: 跑测试确认通过**

```bash
pytest app/tests/unit/test_multi_step_persist_hooks.py -v
pytest app/tests/unit/test_multi_step_retry.py app/tests/unit/test_multi_step_compressor.py -v
```
Expected: 全绿。另跑一次导入冒烟：
```bash
python -c "from app.services.chat_service import ChatService; print(ChatService.__mro__[:4])"
```
Expected: 打印出的 MRO 里能看到 `MultiStepPersistMixin`

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/multi_step_persist_hooks.py \
        backend/app/services/chat_service.py \
        backend/app/config.py \
        backend/app/tests/unit/test_multi_step_persist_hooks.py
git commit -m "feat(multi-step): 新增持久化钩子 mixin + MULTI_STEP_PERSIST_ENABLED 开关"
```

---

### Task 6: 接进多步执行链路（非流式 + 流式）

**Files:**
- Modify: `backend/app/services/chat_multistep.py`（`_executeMultiStep`，仅加钩子调用）
- Modify: `backend/app/services/chat_stream.py`（`_streamMultiStep`，仅加钩子调用）
- Test: `backend/app/tests/integration/test_multi_step_persist_wiring.py`

**Interfaces:**
- Consumes: Task 5 的钩子方法、现有 `_executeDataStep` / `StepExecutionContext.inject_to_prompt`
- Produces: 无新公共接口；行为变化是落库有副作用

**改动纪律:** 只加「打开 run / 每步落库 / 关 run / 压缩判定」四类调用，**不得**改 NL2SQL 逻辑、不得引入模型改派。

- [ ] **Step 1: 写失败测试**

`backend/app/tests/integration/test_multi_step_persist_wiring.py`：
```python
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.domain.multi_step_models import MultiStepRun, MultiStepStep


@pytest.mark.asyncio
async def testMultiStepRunPersistedEndToEnd(pg_client, db_session, monkeypatch):
    """真链路：POST /api/v1/chat 走多步 → multi_step_run/step 落库。"""
    # Arrange：复用 test_chat_api 的 seed + fakes
    from app.tests.integration.test_chat_api import _seed, _installFakes, _chat_payload

    config, datasource = await _seed(db_session)
    _installFakes(monkeypatch, config)
    sessionId = str(uuid.uuid4())

    # Act
    resp = await pg_client.post(
        "/api/v1/chat",
        json=_chat_payload("第一步查总额，第二步查明细", datasource.id, sessionId=sessionId),
    )

    # Assert
    assert resp.status_code == 200
    runs = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.question.like("%第一步%")))
    ).scalars().all()
    assert len(runs) == 1
    steps = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == runs[0].id)
        )
    ).scalars().all()
    assert [s.step_index for s in steps] == [0, 1]
    assert all(s.status == "succeeded" for s in steps)
    assert all(s.sql for s in steps)


@pytest.mark.asyncio
async def testRunMarkedFailedWhenStepExhaustsRetries(pg_client, db_session, monkeypatch):
    """第 2 步 LLM 持续 ConnectError → run.status=failed，第 1 步仍 succeeded。"""
    from app.tests.integration.test_chat_api import _seed, _installFakes, _chat_payload
    import httpx

    config, datasource = await _seed(db_session)
    _installFakes(monkeypatch, config)

    import app.services.llm_retry_policy as retryPolicy
    monkeypatch.setattr(retryPolicy, "RETRY_WAIT_MIN_SECONDS", 0)
    monkeypatch.setattr(retryPolicy, "RETRY_WAIT_MAX_SECONDS", 0)
    monkeypatch.setattr(
        "app.services.multi_step_retry.TRANSIENT_WAITS", (0, 0, 0)
    )

    callCount = {"n": 0}
    service = __import__("app.api.v1.chat", fromlist=["_service"])._service
    original = service._executeDataStep

    async def flakyStep(*args, **kwargs):
        callCount["n"] += 1
        if callCount["n"] >= 2:
            raise httpx.ConnectError("oMLX down")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_executeDataStep", flakyStep)

    sessionId = str(uuid.uuid4())
    resp = await pg_client.post(
        "/api/v1/chat",
        json=_chat_payload("第一步查总额，第二步查明细", datasource.id, sessionId=sessionId),
    )

    assert resp.status_code in (200, 502, 503)
    run = (
        await db_session.execute(
            select(MultiStepRun).where(MultiStepRun.question.like("%第一步%"))
        )
    ).scalars().one()
    assert run.status in ("failed", "partially_failed")
    steps = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == run.id).order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    assert steps[0].status in ("succeeded", "compressed")
    assert steps[-1].attempt_count >= 1


@pytest.mark.asyncio
async def testPersistDisabledWritesNoRows(pg_client, db_session, monkeypatch):
    from app.tests.integration.test_chat_api import _seed, _installFakes, _chat_payload
    from app.config import getSettings
    from types import SimpleNamespace

    config, datasource = await _seed(db_session)
    _installFakes(monkeypatch, config)
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.getSettings",
        lambda: SimpleNamespace(multiStepPersistEnabled=False),
    )

    sessionId = str(uuid.uuid4())
    resp = await pg_client.post(
        "/api/v1/chat",
        json=_chat_payload("第一步查总额，第二步查明细", datasource.id, sessionId=sessionId),
    )

    assert resp.status_code == 200
    runs = (await db_session.execute(select(MultiStepRun))).scalars().all()
    assert runs == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
export TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test
export TEST_NEO4J_URI=bolt://localhost:7688
pytest app/tests/integration/test_multi_step_persist_wiring.py -v
```
Expected: `testMultiStepRunPersistedEndToEnd` FAIL（`assert len(runs) == 1` 得到 0）；`testPersistDisabledWritesNoRows` 可能已 PASS（因为还没接线）。

- [ ] **Step 3: 在 `_executeMultiStep` 接线**

先读现状：
```bash
sed -n '557,600p' backend/app/services/chat_multistep.py
sed -n '690,745p' backend/app/services/chat_multistep.py
```
在 steps 循环**之前**插入：
```python
        subQuestions = [s.description or s.subQuestion for s in multiStepPlan.steps]
        run = await self._openRun(
            session,
            sessionId=dto.sessionId,
            question=dto.question,
            modelId=getattr(dto, "modelId", None),
            subQuestions=subQuestions,
        )
```
（字段名以实际 `multiStepPlan.steps` 的元素属性为准；若为 `StepPlan` 用 `s.sub_question`）

在每步**执行前**：
```python
            if run is not None:
                await persistence.markStepRunning(session, stepsByIdx[index])
```
在每步**执行后（成功）**：
```python
            if run is not None:
                await self._persistStepSuccess(
                    session, stepsByIdx[index],
                    sql=stepRun.sql, data=stepRun.result,
                    chartOption=getattr(stepRun, "chartOption", None),
                    modelUsed=stepRun.modelName,
                    tokens=stepRun.tokens, cost=stepRun.cost,
                )
```
在每步异常分支里（`except` 块）：
```python
            if run is not None:
                await self._persistStepFailure(session, stepsByIdx[index], exc, run=run)
```
循环**之后**：
```python
        await self._closeRun(
            session, run,
            status=runStatusFor(completed, len(multiStepPlan.steps), anyFailed, anySkipped),
            completedSteps=completed,
            currentStepIdx=completed,
        )
        await session.commit()
```
`completed` / `anyFailed` / `anySkipped` 由本步骤自行维护：循环开始前置 `completed = 0; anyFailed = False; anySkipped = False`；每步成功后 `completed += 1`；`except` 分支里若分类为 permanent 置 `anyFailed = True`，若判定为 skip 置 `anySkipped = True`。**不要**改动既有循环里已有的同名局部变量（若已存在，直接复用）。
`stepsByIdx` 的取法：`_openRun` 后立刻
```python
        persisted = await persistence.loadSteps(session, run.id) if run is not None else []
        stepsByIdx = {s.step_index: s for s in persisted}
```
并在 import 区加 `from app.services import multi_step_persistence as persistence` 与
`from app.services.multi_step_persist_hooks import runStatusFor`。

**同时**把每步的 LLM 调用包进瞬态重试：把 `_executeDataStep(...)` 的调用点改为
```python
            stepRun, attempts = await runWithTransientRetry(
                lambda: self._executeDataStep(...原有参数...),
                onError=lambda exc, attempt: self._persistStepFailure(
                    session, stepsByIdx[index], exc, run=run
                )
                if run is not None else _noop(),
            )
```
若原调用点参数复杂，退而求其次：保留原调用，只在异常分支接 `_persistStepFailure`（自动重试仍生效，因为 `_executeDataStep` 内部的 `_runQueryWithRetry` 已有瞬态重试）。**二选一并在此步骤的注释里写明选了哪个。**

- [ ] **Step 4: 在 `_streamMultiStep` 接线（同一套钩子）**

```bash
grep -n "_streamMultiStep\|_executeDataStep\|MultiStepPlan\|inject_to_prompt" backend/app/services/chat_stream.py | head -30
```
在流式版循环里加与 Step 3 相同的 `_openRun` / `markStepRunning` / `_persistStepSuccess` / `_persistStepFailure` / `_closeRun`。压缩判定插在 `inject_to_prompt` 调用**之前**：
```python
        injectionText = stepContext.inject_to_prompt(index)
        await self._maybeCompressPriorSteps(
            session, run, steps, nextStepIdx=index,
            maxInputTokens=_maxInputTokens(pc), injectionText=injectionText,
        )
```
非流式（Step 3）同样插入这段。
`_maxInputTokens(pc)` 用一行 helper 取当前 model 配置的上限（找不到时返回 `0`，`shouldCompress` 会安全地返回 False）：
```python
def _maxInputTokens(pipelineContext) -> int:
    for cfg in getattr(pipelineContext, "configs", ()) or ():
        if getattr(cfg, "selected", False):
            return int(getattr(cfg, "max_input_tokens", 0) or 0)
    return 0
```

- [ ] **Step 5: 跑测试确认通过**

```bash
pytest app/tests/integration/test_multi_step_persist_wiring.py -v
```
Expected: 3 passed

- [ ] **Step 6: 回归既有 chat 套件**

```bash
pytest app/tests/integration/test_chat_multi_step.py app/tests/integration/test_multistep_global_filter.py -v
```
Expected: 与基线一致（无新增红）。若出现红，先判断是否为本计划引入，**不要**顺手改无关测试。

- [ ] **Step 7: 确认行数未超限**

```bash
wc -l backend/app/services/chat_multistep.py backend/app/services/chat_stream.py backend/app/services/multi_step_persist_hooks.py
```
Expected: `chat_multistep.py` < 1000。若逼近，把 Step 3/4 的重复段落抽成 mixin 方法。

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/chat_multistep.py backend/app/services/chat_stream.py \
        backend/app/tests/integration/test_multi_step_persist_wiring.py
git commit -m "feat(multi-step): 执行链路接入落库/重试/压缩钩子"
```

---

### Task 7: 续跑 API

**Files:**
- Create: `backend/app/services/multi_step_resume.py`
- Modify: `backend/app/api/v1/chat.py`（新增路由）
- Test: `backend/app/tests/integration/test_multi_step_resume_api.py`

**Interfaces:**
- Consumes: Task 2 仓储、Task 5 钩子、现有 `assertSessionOwnership` / `getCurrentUser` / `_service.processMessageStream`
- Produces:
  - `class MultiStepResumeService`，方法 `async def resume(self, session, *, runId: uuid.UUID, userId: Any, fromStepIndex: int | None, modelOverride: int | None, compressAgain: bool, idempotencyKey: str | None) -> AsyncIterator[...]`
  - HTTP：`POST /api/v1/chat/multi-step/{runId}/resume`

- [ ] **Step 1: 写失败测试**

`backend/app/tests/integration/test_multi_step_resume_api.py`:
```python
import uuid

import pytest
from sqlalchemy import select

from app.domain.multi_step_models import MultiStepRun, MultiStepStep


@pytest.mark.asyncio
async def testResumeRejectsNonFailedRun(pg_client, db_session):
    # Arrange：一条 succeeded 的 run
    from app.domain.research_models import ResearchSession
    from app.services import multi_step_persistence as repo

    sessionRow = ResearchSession(id=uuid.uuid4(), title="resume-1", created_by=1)
    db_session.add(sessionRow)
    await db_session.commit()
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="q", modelId=1, totalSteps=1
    )
    await repo.updateRun(db_session, run, status="succeeded", finished=True)
    await db_session.commit()

    # Act
    resp = await pg_client.post(f"/api/v1/chat/multi-step/{run.id}/resume", json={})

    # Assert
    assert resp.status_code == 409


@pytest.mark.asyncio
async def testResumeFromStepResetsLaterSteps(pg_client, db_session, monkeypatch):
    from app.domain.research_models import ResearchSession
    from app.services import multi_step_persistence as repo
    from app.tests.integration.test_chat_api import _seed, _installFakes

    config, _datasource = await _seed(db_session)
    _installFakes(monkeypatch, config)

    sessionRow = ResearchSession(id=uuid.uuid4(), title="resume-2", created_by=1)
    db_session.add(sessionRow)
    await db_session.commit()
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="两步题", modelId=config.id, totalSteps=2
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A", "查B"])
    await repo.finishStep(db_session, steps[0], status="succeeded", sql="SELECT 1", data=[{"a": 1}])
    await repo.recordStepError(db_session, steps[1], message="timeout", kind="transient")
    await repo.updateRun(db_session, run, status="failed", completedSteps=1, finished=True)
    await db_session.commit()

    # Act
    resp = await pg_client.post(
        f"/api/v1/chat/multi-step/{run.id}/resume",
        json={"from_step_index": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    # Assert
    assert resp.status_code == 200
    reloaded = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == run.id).order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    assert reloaded[1].last_error is None
    assert reloaded[1].status in ("pending", "running", "succeeded")
    reloadedRun = (await db_session.execute(select(MultiStepRun).where(MultiStepRun.id == run.id))).scalar_one()
    await db_session.refresh(reloadedRun)
    assert reloadedRun.resume_count >= 1


@pytest.mark.asyncio
async def testResumeIsIdempotentOnSameKey(pg_client, db_session, monkeypatch):
    from app.domain.research_models import ResearchSession
    from app.services import multi_step_persistence as repo
    from app.tests.integration.test_chat_api import _seed, _installFakes

    config, _ds = await _seed(db_session)
    _installFakes(monkeypatch, config)
    sessionRow = ResearchSession(id=uuid.uuid4(), title="resume-3", created_by=1)
    db_session.add(sessionRow)
    await db_session.commit()
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="q", modelId=config.id, totalSteps=1
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A"])
    await repo.recordStepError(db_session, steps[0], message="x", kind="transient")
    await repo.updateRun(db_session, run, status="failed", finished=True)
    await db_session.commit()

    key = str(uuid.uuid4())
    first = await pg_client.post(
        f"/api/v1/chat/multi-step/{run.id}/resume", json={}, headers={"Idempotency-Key": key}
    )
    second = await pg_client.post(
        f"/api/v1/chat/multi-step/{run.id}/resume", json={}, headers={"Idempotency-Key": key}
    )

    assert first.status_code == 200
    assert second.status_code in (200, 409)


@pytest.mark.asyncio
async def testResumeUnknownRunReturns404(pg_client):
    resp = await pg_client.post(f"/api/v1/chat/multi-step/{uuid.uuid4()}/resume", json={})
    assert resp.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test
export TEST_NEO4J_URI=bolt://localhost:7688
pytest app/tests/integration/test_multi_step_resume_api.py -v
```
Expected: FAIL — 全部 404（路由不存在）

- [ ] **Step 3: 写续跑服务**

`backend/app/services/multi_step_resume.py`:
```python
"""多步续跑（spec §7）。校验 → 重置后续步 → 复用流式执行。"""
from __future__ import annotations

import logging
import uuid
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.multi_step_models import RUN_STATUS_FAILED, RUN_STATUS_PARTIALLY_FAILED
from app.services import multi_step_persistence as persistence

logger = logging.getLogger(__name__)

RESUMABLE_STATUSES = frozenset({RUN_STATUS_FAILED, RUN_STATUS_PARTIALLY_FAILED})


class ResumeConflict(Exception):
    """并发续跑或状态不允许。"""


class ResumeNotAllowed(Exception):
    """run 不存在或不属于该用户。"""


async def prepareResume(
    session: AsyncSession,
    *,
    runId: uuid.UUID,
    fromStepIndex: int | None,
    idempotencyKey: str | None,
) -> tuple[Any, int]:
    """校验并重置；返回 (run, 起始步号)。"""
    run = await persistence.loadRun(session, runId)
    if run is None:
        raise ResumeNotAllowed(f"run {runId} not found")

    if idempotencyKey and idempotencyKey in (run.idempotency_keys or []):
        raise ResumeConflict("duplicate idempotency key")
    if run.status not in RESUMABLE_STATUSES:
        raise ResumeConflict(f"run status {run.status} not resumable")

    steps = await persistence.loadSteps(session, runId)
    start = fromStepIndex
    if start is None:
        start = next(
            (s.step_index for s in steps if s.status in ("failed", "skipped")),
            0,
        )
    if start < 0 or start >= len(steps):
        raise ResumeConflict(f"from_step_index {start} out of range 0..{len(steps) - 1}")
    for step in steps:
        if step.step_index < start and step.status not in ("succeeded", "compressed"):
            raise ResumeConflict(f"step {step.step_index} not completed; cannot resume from {start}")

    await persistence.resetStepsFrom(session, runId=runId, fromStepIndex=start)
    if idempotencyKey:
        await persistence.appendIdempotencyKey(session, run, idempotencyKey)
    run.resume_count = (run.resume_count or 0) + 1
    run.version = (run.version or 0) + 1
    run.status = "running"
    run.finished_at = None
    run.current_step_idx = start
    await session.commit()
    return run, start
```

- [ ] **Step 4: 写路由**

**先确认 DTO 命名约定**（否则前后端对不上、`extra=forbid` 会直接 422）：
```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
grep -n "class ChatRequest" -A 25 app/models/schemas.py | head -35
grep -rn "class ChatRequest\|class CamelModel\|class ResumeRequest" app/
```
若 `ChatRequest` 继承自 `CamelModel`（字段形如 `sessionId` / `datasourceId`），则 `ResumeRequest` 必须同样用 `CamelModel` + snake_case 字段声明（由 `CamelModel` 序列化成 camelCase），且前端 body 发 camelCase；若 `ChatRequest` 是纯 snake_case，则统一 snake_case。**以实测为准**，下面的正文按 camelCase 约定给出。

`backend/app/api/v1/chat.py`，在 `suggestQueries` 之后追加：
```python
class ResumeRequest(CamelModel):
    from_step_index: int | None = None
    model_override: int | None = None
    compress_again: bool = False


@router.post("/multi-step/{runId}/resume")
async def resumeMultiStep(
    request: Request,
    runId: uuid.UUID,
    dto: ResumeRequest,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    run = await multi_step_persistence.loadRun(session, runId)
    if run is None:
        raise NotFoundError(f"multi-step run {runId} 不存在")
    await assertSessionOwnership(session, str(run.session_id), _user)

    idempotencyKey = request.headers.get("Idempotency-Key")
    try:
        _, startIndex = await multi_step_resume.prepareResume(
            session, runId=runId, fromStepIndex=dto.from_step_index,
            idempotencyKey=idempotencyKey,
        )
    except multi_step_resume.ResumeConflict as exc:
        raise ConflictError(str(exc)) from exc

    chatDto = ChatRequest(
        question=run.question,
        sessionId=str(run.session_id),
        datasourceId=_datasourceIdFromRun(run),
        modelId=dto.model_override or run.model_id,
        resumeFromStep=startIndex,
    )

    async def eventSource() -> AsyncIterator[str]:
        async for event in _service.processMessageStream(chatDto, session, user=_user):
            yield event.toSse()

    return StreamingResponse(
        eventSource(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```
补 import：
```python
import uuid
from app.domain.exceptions import ConflictError, NotFoundError
from app.models.schemas import ChatRequest  # 以实际 ChatRequest 所在模块为准
from app.services import multi_step_persistence, multi_step_resume
```
并在 `ChatRequest` 上加可选字段 `resumeFromStep: int | None = None`（若已存在同名则复用）。
`_datasourceIdFromRun` 暂时从 run 的 session 最近 query state 取数据源 id；若取不到，返回 `run.question` 就无从执行 → 改为在 `multi_step_run` 增列 `datasource_id INT`（Task 1 的表已可加列，见 Step 5）。

- [ ] **Step 5: 补 `datasource_id` 列（若 Step 4 需要）**

在 Task 1 的模型与迁移里加：
```python
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
```
迁移里加 `sa.Column("datasource_id", sa.Integer(), nullable=True)`。
在 `_openRun` 里接收并写入 `datasourceId`（从 `dto.datasourceId`）。
然后 `_datasourceIdFromRun(run)` 直接 `return run.datasource_id`。

- [ ] **Step 6: 跑测试确认通过**

```bash
pytest app/tests/integration/test_multi_step_resume_api.py -v
```
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/multi_step_resume.py backend/app/api/v1/chat.py \
        backend/app/domain/multi_step_models.py backend/alembic/versions/0114_multi_step_persist.py \
        backend/app/tests/integration/test_multi_step_resume_api.py
git commit -m "feat(multi-step): 新增续跑 API POST /chat/multi-step/{runId}/resume"
```

---

### Task 8: 清理任务

**Files:**
- Create: `backend/app/jobs/cleanup_multi_step_runs.py`
- Test: `backend/app/tests/integration/test_multi_step_cleanup.py`

**Interfaces:**
- Produces: `async def cleanupMultiStepRuns(session, *, succeededRetentionDays: int = 30, failedRetentionDays: int = 7, now: datetime | None = None) -> int`

- [ ] **Step 1: 写失败测试**

`backend/app/tests/integration/test_multi_step_cleanup.py`:
```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.domain.multi_step_models import MultiStepRun
from app.jobs.cleanup_multi_step_runs import cleanupMultiStepRuns


@pytest.mark.asyncio
async def testCleanupDeletesOnlyExpiredRuns(db_session):
    from app.domain.research_models import ResearchSession

    sessionRow = ResearchSession(id=uuid.uuid4(), title="cleanup", created_by=1)
    db_session.add(sessionRow)
    await db_session.commit()

    now = datetime.now(UTC)

    async def addRun(status: str, ageDays: int) -> MultiStepRun:
        run = MultiStepRun(
            id=uuid.uuid4(), session_id=sessionRow.id, question="q", model_id=None,
            total_steps=1, status=status,
            started_at=now - timedelta(days=ageDays),
            updated_at=now - timedelta(days=ageDays),
            finished_at=now - timedelta(days=ageDays),
        )
        db_session.add(run)
        await db_session.flush()
        return run

    oldSucceeded = await addRun("succeeded", 40)   # 删
    freshSucceeded = await addRun("succeeded", 5)  # 留
    oldFailed = await addRun("failed", 10)         # 删
    freshFailed = await addRun("failed", 2)        # 留
    oldRunning = await addRun("running", 100)      # 留（未终态不删）
    await db_session.commit()

    # Act
    deleted = await cleanupMultiStepRuns(db_session, now=now)
    await db_session.commit()

    # Assert
    assert deleted == 2
    remaining = {r.id for r in (await db_session.execute(select(MultiStepRun))).scalars().all()}
    assert remaining == {freshSucceeded.id, freshFailed.id, oldRunning.id}


@pytest.mark.asyncio
async def testCleanupDeletesStepsViaCascade(db_session):
    from app.domain.multi_step_models import MultiStepStep
    from app.domain.research_models import ResearchSession

    sessionRow = ResearchSession(id=uuid.uuid4(), title="cleanup-cascade", created_by=1)
    db_session.add(sessionRow)
    await db_session.commit()
    now = datetime.now(UTC)
    run = MultiStepRun(
        id=uuid.uuid4(), session_id=sessionRow.id, question="q", model_id=None,
        total_steps=1, status="succeeded",
        started_at=now - timedelta(days=60), updated_at=now - timedelta(days=60),
        finished_at=now - timedelta(days=60),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(MultiStepStep(id=uuid.uuid4(), run_id=run.id, step_index=0, status="succeeded", sub_question="a"))
    await db_session.commit()

    await cleanupMultiStepRuns(db_session, now=now)
    await db_session.commit()

    assert (await db_session.execute(select(MultiStepStep))).scalars().all() == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest app/tests/integration/test_multi_step_cleanup.py -v
```
Expected: FAIL — `ModuleNotFoundError: app.jobs.cleanup_multi_step_runs`

- [ ] **Step 3: 写实现**

`backend/app/jobs/cleanup_multi_step_runs.py`:
```python
"""多步 run 保留期清理（spec §10.4）。成功 30 天，失败/部分失败 7 天，未终态不删。"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.multi_step_models import (
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIALLY_FAILED,
    RUN_STATUS_SUCCEEDED,
    MultiStepRun,
)

logger = logging.getLogger(__name__)

SUCCEEDED_RETENTION_DAYS = 30
FAILED_RETENTION_DAYS = 7

_TERMINAL = (RUN_STATUS_SUCCEEDED, RUN_STATUS_FAILED, RUN_STATUS_PARTIALLY_FAILED)


async def cleanupMultiStepRuns(
    session: AsyncSession,
    *,
    succeededRetentionDays: int = SUCCEEDED_RETENTION_DAYS,
    failedRetentionDays: int = FAILED_RETENTION_DAYS,
    now: datetime | None = None,
) -> int:
    reference = now or datetime.now(UTC)
    succeededBefore = reference - timedelta(days=succeededRetentionDays)
    failedBefore = reference - timedelta(days=failedRetentionDays)

    expiredIds = (
        await session.execute(
            select(MultiStepRun.id).where(
                MultiStepRun.status.in_(_TERMINAL),
                or_(
                    (MultiStepRun.status == RUN_STATUS_SUCCEEDED)
                    & (MultiStepRun.finished_at < succeededBefore),
                    MultiStepRun.status.in_((RUN_STATUS_FAILED, RUN_STATUS_PARTIALLY_FAILED))
                    & (MultiStepRun.finished_at < failedBefore),
                ),
            )
        )
    ).scalars().all()

    if not expiredIds:
        return 0
    await session.execute(delete(MultiStepRun).where(MultiStepRun.id.in_(expiredIds)))
    logger.info("多步 run 清理：删除 %d 条（截止 %s）", len(expiredIds), reference.isoformat())
    return len(expiredIds)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest app/tests/integration/test_multi_step_cleanup.py -v
```
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/jobs/cleanup_multi_step_runs.py backend/app/tests/integration/test_multi_step_cleanup.py
git commit -m "feat(multi-step): 新增 run 保留期清理任务"
```

---

### Task 9: 前端续跑入口

**Files:**
- Modify: `frontend/src/` 聊天面板组件（先定位：`grep -rn "step_result\|multi_step_plan" frontend/src`）
- Modify: 对应 i18n 文案文件
- Test: 对应 `*.test.tsx`

**Interfaces:**
- Consumes: `POST /api/v1/chat/multi-step/{runId}/resume`；SSE 事件里新增的 run/step 状态（若后端未下发 runId，则从 `multi_step_plan` 事件扩展）
- Produces: 组件 `ResumeRunButton`；step 卡片状态行

- [ ] **Step 1: 定位现有 step 渲染组件与 SSE 消费点**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
grep -rn "step_result\|EVENT_STEP_RESULT\|multi_step_plan" frontend/src --include=*.ts --include=*.tsx | head -20
grep -rn "EventSource\|fetchEventSource\|text/event-stream" frontend/src --include=*.ts --include=*.tsx | head -10
```

- [ ] **Step 2: 写失败测试**

在定位到的组件测试文件里加：
```tsx
it('失败步骤渲染续跑按钮并在点击时调用 resume 接口', async () => {
  // Arrange
  const onResume = vi.fn();
  render(<StepCard step={{ index: 1, status: 'failed', subQuestion: '查B', lastError: 'oMLX timeout' }} runId="r-1" onResume={onResume} />);

  // Act
  await userEvent.click(screen.getByRole('button', { name: /续跑/ }));

  // Assert
  expect(onResume).toHaveBeenCalledWith('r-1', 1);
});

it('压缩步骤显示压缩徽章与原始行数', () => {
  // Arrange & Act
  render(<StepCard step={{ index: 0, status: 'compressed', originalRows: 1000, compressedRows: 30 }} runId="r-1" />);

  // Assert
  expect(screen.getByText(/已压缩/)).toBeInTheDocument();
  expect(screen.getByText(/1000/)).toBeInTheDocument();
});
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd frontend && npm test -- --run <组件测试路径>
```
Expected: FAIL — `ResumeRunButton` / `StepCard` 新 props 未定义

- [ ] **Step 4: 实现组件**

`ResumeRunButton.tsx`（约 40 行）：
```tsx
type Props = { runId: string; disabled?: boolean; onResume: (runId: string, fromStepIndex?: number) => void };

export function ResumeRunButton({ runId, disabled, onResume }: Props) {
  const { t } = useTranslation();
  return (
    <Button size="small" type="primary" disabled={disabled}
      onClick={() => onResume(runId)} data-testid="resume-run">
      {t('chat.multiStep.resume')}
    </Button>
  );
}
```
`StepCard` 状态行按 `status` 分支渲染：`succeeded` → `✓ 成功 · {tokens} token`；`compressed` → `⚠️ 数据已压缩（{originalRows} → {compressedRows} 行）+ 展开原始数据`；`failed` → `✗ 失败 · {lastError} + <ResumeRunButton/>`。
i18n 文案（注意本项目 i18n **单花括号**插值，见 memory `qa-system-i18n-single-brace`）：
```json
"chat": { "multiStep": { "resume": "续跑", "compressed": "数据已压缩（{from} → {to} 行）", "failed": "失败" } }
```
续跑调用（复用既有 SSE fetch 鉴权封装，见 memory `qa-system-frontend-sse-auth-header`）：
```ts
export async function resumeMultiStepRun(runId: string, fromStepIndex: number, signal?: AbortSignal) {
  return fetchSse(`/api/v1/chat/multi-step/${runId}/resume`, {
    method: 'POST',
    body: JSON.stringify({ from_step_index: fromStepIndex }),
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    signal,
  });
}
```

- [ ] **Step 5: 跑测试确认通过 + 覆盖率门禁**

```bash
cd frontend && npm test -- --run && npm run test:coverage
```
Expected: 新测试通过；全局覆盖率不低于门禁（见 memory `qa-system-frontend-coverage-gate`）

- [ ] **Step 6: Commit**

```bash
git add frontend/src
git commit -m "feat(multi-step): 前端续跑按钮与步骤状态徽章"
```

---

### Task 10: 文档与变更记录

**Files:**
- Create: `Harness/wiki/chat_multi_step_persistence.md`
- Create: `Harness/changes/feat-multi-step-persist/summary.md`
- Modify: `Harness/index.md`（若存在索引则登记新条目）

- [ ] **Step 1: 写 wiki 条目**

`Harness/wiki/chat_multi_step_persistence.md`：按 `Harness/wiki/` 既有条目格式（frontmatter + 概述 + 详细说明 + 相关条目），内容涵盖：两张表的关系、状态机、压缩触发阈值 0.7、重试 3 次 1s/2s/4s、续跑端点与幂等、保留期 30/7 天、feature flag 名。链接 [[chat_multistep_flow]]、[[llm_retry_policy]]、[[research_session]]（按实际存在的条目名调整）。

- [ ] **Step 2: 写 change 记录**

`Harness/changes/feat-multi-step-persist/summary.md`：SSOT 记录，含 spec 链接、迁移号 0114、feature flag、新增文件清单、测试命令与结果、遗留项（数据源 id 快照、压缩后仍超限转 skipped 的实现位置）。

- [ ] **Step 3: Commit**

```bash
git add Harness/wiki/chat_multi_step_persistence.md Harness/changes/feat-multi-step-persist/summary.md Harness/index.md
git commit -m "docs(multi-step): 补 wiki 与 change 记录"
```

---

## 遗留项（本计划不做，但要在 change 记录里登记）

1. **压缩后仍 > 95% → 步转 `skipped`**：spec §4.1 定义了该状态，但触发点依赖 `_planAndGenerateSql` 的实际报错形态；先按 permanent 处理，观察线上日志后再实现。
2. **`compress_again` 参数**：API 已接收但当前实现忽略（压缩只按阈值自动触发）。
3. **前端 `from_step_index` 选择弹窗**：Task 9 只做「默认从首个失败步续跑」；下拉选步延后。
4. **`sql_hash` 命中缓存跳过 LLM**：字段已落库，但续跑时尚未用它跳过生成 —— 先保证正确性，再优化 token。
5. **SSE 中断后前端自动重连续跑**：依赖前端的 SSE 封装改造，单独排期。
6. **超大 data（> 5MB）转对象存储**：spec §14 提到超限走 minio，但当前 `data` 一律进 JSONB。先观察真实 `pg_column_size(multi_step_step.data)` 分布，确认有超限样本后再实现，避免过早引入存储依赖。
7. **并发续跑乐观锁的落库侧强约束**：当前靠 `run.version++` 的自增语义 + 状态校验挡住大部分并发，但**没有** `SELECT … FOR UPDATE`，极端并发下两个请求都可能通过校验。若线上出现双跑，再补行级锁。
