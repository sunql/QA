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
  - `MultiStepRun`（字段 `id, session_id, question, model_id, datasource_id, status, total_steps, completed_steps, current_step_idx, compressed_count, resume_count, version, idempotency_keys, error_summary, started_at, updated_at, finished_at`）
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
        datasource_id=7,
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
    assert loaded.datasource_id == 7
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
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
        sa.Column("datasource_id", sa.Integer(), nullable=True),
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
  - `createRun(session, *, sessionId: uuid.UUID, question: str, modelId: int | None, datasourceId: int | None = None, totalSteps: int) -> MultiStepRun`
  - `createSteps(session, *, runId: uuid.UUID, subQuestions: list[str]) -> list[MultiStepStep]`
  - `markStepRunning(session, step: MultiStepStep) -> None`
  - `finishStep(session, step, *, status: str, sql: str | None = None, data: list | None = None, chartOption: dict | None = None, modelUsed: str | None = None, tokens: int = 0, cost: float = 0) -> None`
  - `recordStepError(session, step, *, message: str, kind: str, tokens: int = 0, cost: float = 0) -> None`（失败尝试的用量累加进本步，不覆盖）
  - `updateRun(session, run, *, status: str | None = None, completedSteps: int | None = None, currentStepIdx: int | None = None, compressedCount: int | None = None, errorSummary: str | None = None, finished: bool = False) -> None`
  - `loadRun(session, runId: uuid.UUID) -> MultiStepRun | None`
  - `loadSteps(session, runId: uuid.UUID) -> list[MultiStepStep]`（按 `step_index` 升序）
  - `resetStepsFrom(session, *, runId: uuid.UUID, fromStepIndex: int) -> int`
  - `appendIdempotencyKey(session, run) -> None`（仅当 key 未在列表中时追加）

- [ ] **Step 1: 写失败测试**

`backend/app/tests/integration/test_multi_step_persist_repo.py`:
```python
import uuid
from decimal import Decimal

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
    await repo.recordStepError(
        db_session, step, message="timeout", kind="transient", tokens=12, cost=0.0002
    )
    await repo.recordStepError(db_session, step, message="timeout again", kind="transient")
    await db_session.commit()

    loaded = (await repo.loadSteps(db_session, run.id))[0]
    assert loaded.attempt_count == 2
    assert loaded.last_error == "timeout again"
    assert loaded.last_error_kind == "transient"
    assert loaded.status == "running"  # 未终态
    # 失败尝试的用量累加不覆盖（spec §6.2）；第二次未传用量即按默认 0 处理
    assert loaded.tokens_used == 12
    assert loaded.cost == Decimal("0.0002")


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
    datasourceId: int | None = None,
    totalSteps: int,
) -> MultiStepRun:
    if totalSteps < 0:
        raise ValueError(f"totalSteps must be >= 0, got {totalSteps}")
    run = MultiStepRun(
        id=uuid.uuid4(),
        session_id=sessionId,
        question=question,
        model_id=modelId,
        datasource_id=datasourceId,
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
    session: AsyncSession,
    step: MultiStepStep,
    *,
    message: str,
    kind: str,
    tokens: int = 0,
    cost: float = 0,
) -> None:
    """记录一次失败尝试。

    tokens/cost 是该次尝试已消耗的用量（默认 0），累加进本步、不覆盖
    （spec §6.2「每次重试 tokens_used / cost 累加」）。调用方拿不到用量时留空，
    不要为了凑数传假值。
    状态保持 running：本函数是 per-attempt 语义，步的终态（failed / skipped）
    由执行链路在判定终止时落库（spec §6.3）。
    """
    step.attempt_count = (step.attempt_count or 0) + 1
    step.last_error = message[:2000]
    step.last_error_kind = kind
    step.tokens_used = (step.tokens_used or 0) + tokens
    step.cost = Decimal(str(step.cost or 0)) + Decimal(str(cost))
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
- Consumes: `app.domain.exceptions.LLMUnavailableError`、`LlmClientError`、`Nl2SqlError`
- Produces:
  - 常量 `TRANSIENT_WAITS = (1, 2)`、`MAX_ATTEMPTS = 3`（**两者各自独立定义，勿写 `MAX_ATTEMPTS = len(TRANSIENT_WAITS)`**）、`ERROR_KIND_TRANSIENT = "transient"`、`ERROR_KIND_PERMANENT = "permanent"`
  - `classifyStepError(exc: BaseException) -> str`
  - `async def runWithTransientRetry(call, *, sleep=asyncio.sleep, onError=None) -> tuple[T, int]`

**设计说明（与既有策略的差异，必须写进 docstring）:** 不复用 `llm_retry_policy.isRetryableLlmError`——它对 `Nl2SqlError` 一律返回可重试，而多步场景下 plan 校验失败属于**永久**错误（重试无意义、白烧 token）。本模块自带分类器。

**分类必须沿整条 `__cause__` 链走（2026-10-05 人类裁决，spec §6.1 同步修订）：** 本仓所有 provider 失败都被 `openai_client` 以 `LlmClientError(...) from exc` 包住，故 spec §6.1 列的 httpx 类型在分类点**永远不会裸着到达**；只看最外层类型、或只看一层 `__cause__`，会把「provider 不可达 / 超时」误判为永久——而那正是本功能要救的故障类别。硬性要求：
- 遍历 `__cause__`/`__context__` 整条链（按 id 去环），**自外向内、首个能判定的环生效**——该环命中瞬态类型即 `transient`；该环带状态码则按其值判定（`429/500/502/503/504` → `transient`，其余 → `permanent`）；两者皆无则继续深入。现实链路中「带状态码的环」与「传输层瞬态环」不会互相嵌套，故与「全链扫描」结果等价；此处刻意取首环生效（规则确定、无需两遍扫描）；
- 瞬态类型 = `httpx.TransportError`（已含 ConnectError / TimeoutException / ReadError / RemoteProtocolError / PoolTimeout）、内建 `ConnectionError`、`asyncio.TimeoutError`；
- 状态码读 `status_code` 或 `status`（aiohttp 一类客户端用后者），`429/500/502/503/504` → `transient`，其余 → `permanent`；
- `Nl2SqlError` 与 `LLMUnavailableError` 按类型**优先**判永久（前者语义固定；后者是「未配置 LLM」配置错，重试不自愈）。

- [ ] **Step 1: 写失败测试**

`backend/app/tests/unit/test_multi_step_retry.py`:
```python
import asyncio

import httpx
import pytest

from app.domain.exceptions import LLMUnavailableError, LlmClientError, Nl2SqlError
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


def _wrap(inner: Exception, outer: Exception) -> Exception:
    """返回以 ``inner`` 为 __cause__ 的 ``outer``（等价于 `raise outer from inner`）。"""
    try:
        raise inner
    except Exception as exc:  # noqa: BLE001 - 仅用于构造异常链
        try:
            raise outer from exc
        except Exception as wrapped:  # noqa: BLE001 - 同上
            return wrapped


@pytest.mark.parametrize(
    "exc, expected",
    [
        # 裸类型（本模块自身可能直接看到）
        (httpx.ConnectError("refused"), ERROR_KIND_TRANSIENT),
        (httpx.ReadTimeout("slow"), ERROR_KIND_TRANSIENT),
        (asyncio.TimeoutError(), ERROR_KIND_TRANSIENT),
        (_StatusError(429), ERROR_KIND_TRANSIENT),
        (_StatusError(502), ERROR_KIND_TRANSIENT),
        (_StatusError(503), ERROR_KIND_TRANSIENT),
        (_StatusError(400), ERROR_KIND_PERMANENT),
        (Nl2SqlError("plan 校验失败"), ERROR_KIND_PERMANENT),
        (ValueError("bad input"), ERROR_KIND_PERMANENT),
        # 配置错误（未配置 LLM / 无可用 key）：重试不会自愈 → 永久
        (LLMUnavailableError("no client"), ERROR_KIND_PERMANENT),
        # 真实链路形态：provider 失败被 LlmClientError 包住（openai_client 的 `from exc`）
        (
            _wrap(httpx.ConnectError("refused"), LlmClientError("call failed", provider="openai")),
            ERROR_KIND_TRANSIENT,
        ),
        (
            _wrap(ConnectionResetError("reset by peer"), LlmClientError("call failed", provider="openai")),
            ERROR_KIND_TRANSIENT,
        ),
        (
            _wrap(_StatusError(503), LlmClientError("call failed", provider="openai")),
            ERROR_KIND_TRANSIENT,
        ),
        (
            _wrap(_StatusError(401), LlmClientError("call failed", provider="openai")),
            ERROR_KIND_PERMANENT,
        ),
        # 状态码埋在第二层 __cause__ 之下：只走一层会漏判
        (
            _wrap(_wrap(_StatusError(503), Exception("middle")), LlmClientError("call failed")),
            ERROR_KIND_TRANSIENT,
        ),
        # 无 cause 的 LlmClientError（缺 endpoint / key 等配置错）→ 永久
        (LlmClientError("missing endpoint", provider="azure"), ERROR_KIND_PERMANENT),
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
from collections.abc import Awaitable, Callable, Iterator
from typing import TypeVar

import httpx

from app.domain.exceptions import LLMUnavailableError, Nl2SqlError

logger = logging.getLogger(__name__)

ERROR_KIND_TRANSIENT = "transient"
ERROR_KIND_PERMANENT = "permanent"

#: 第 N 次尝试失败后等 TRANSIENT_WAITS[N - 1] 秒。3 次尝试之间只等 2 次
#: （spec §6.2：1s → 2s → 第 3 次失败即转 manual），故只有 2 个元素。
TRANSIENT_WAITS: tuple[int, ...] = (1, 2)
#: 尝试次数上限。**独立于 TRANSIENT_WAITS 的长度**——写成 len(TRANSIENT_WAITS)
#: 会让「等待次数」与「尝试次数」互相绑死（长度 2 会被误读成最多试 2 次），
#: 正是本模块要避免的坑。
MAX_ATTEMPTS: int = 3

#: 判定「瞬态」的 http 状态码：429 限流 + 5xx 服务端错误。
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

#: 直接判瞬态的异常类型。`httpx.TransportError` 已覆盖 ConnectError /
#: TimeoutException / ReadError / RemoteProtocolError / PoolTimeout 等全部传输层
#: 失败（它们都没有 http 状态码，重试是正确处置）；内建 `ConnectionError`
#: 覆盖 socket 层的连接重置/拒绝。
_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    ConnectionError,
    asyncio.TimeoutError,
)

T = TypeVar("T")


def classifyStepError(exc: BaseException) -> str:
    """把异常分成 transient（可自动重试）或 permanent（转人工）。

    **沿整条 `__cause__`/`__context__` 链判定**，不只看最外层：本仓所有 provider
    失败都被 `openai_client` 以 `LlmClientError(...) from exc` 包住，故 spec §6.1
    列的 httpx 类型在分类点永远不会裸着到达；只看一层会把「provider 不可达 /
    超时」误判为永久，而那正是本功能要救的故障类别。HTTP 状态码同理，也可能
    埋在多层之下。

    与 `llm_retry_policy.isRetryableLlmError` 的差异：那一条对 `Nl2SqlError` 一律
    返回可重试，而多步场景下 plan 校验失败属永久错误（重试白烧 token），故不复用。
    """
    # 按类型优先判永久：语义固定，不受包装层数影响。
    if isinstance(exc, (Nl2SqlError, LLMUnavailableError)):
        # Nl2SqlError：NL2SQL 自带重试/降级，plan 校验失败重试无意义。
        # LLMUnavailableError：「未配置 LLM / 无可用 key」是配置错，重试不自愈。
        return ERROR_KIND_PERMANENT

    for link in _causeChain(exc):
        if isinstance(link, _TRANSIENT_TYPES):
            return ERROR_KIND_TRANSIENT
        status = _statusCodeOf(link)
        if status is not None:
            return ERROR_KIND_TRANSIENT if status in _TRANSIENT_STATUS else ERROR_KIND_PERMANENT
    return ERROR_KIND_PERMANENT


def _causeChain(exc: BaseException) -> Iterator[BaseException]:
    """异常自身 + 整条 `__cause__`／`__context__` 链（按 id 去环）。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _statusCodeOf(exc: BaseException) -> int | None:
    """取链接上的 http 状态码（`status` 是 aiohttp 一类客户端的字段名）。"""
    for attr in ("status_code", "status"):
        code = getattr(exc, attr, None)
        if isinstance(code, int) and not isinstance(code, bool):
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
            # 最后一次尝试失败后不再等待，直接转人工（spec §6.2）
            if attempt < MAX_ATTEMPTS:
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
Expected: 21 passed（`testClassifyStepError` 16 个参数化用例 + 5 个测试函数）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/multi_step_retry.py backend/app/tests/unit/test_multi_step_retry.py
git commit -m "feat(multi-step): 新增错误分类与瞬态重试（1s/2s 退避，3 次封顶）"
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
    （返回值保证 JSON 原生：`Decimal`→`float`、`datetime`/`date`→`isoformat()`，
    因为 `data_compressed` 是裸 JSONB 列、无 `default=str` 编码器）
  - `estimatePromptTokens(text: str) -> int`
  - `shouldCompress(estimatedTokens: int, maxInputTokens: int, *, threshold: float = COMPRESS_THRESHOLD) -> bool`

- [ ] **Step 1: 写失败测试**

`backend/app/tests/unit/test_multi_step_compressor.py`:
```python
import json
from datetime import datetime
from decimal import Decimal

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


def testCompressAggregatesDecimalColumnsAndEmitsJsonSafeOutput():
    # 业务库数值列常以 Decimal 返回（同 data_summary._to_float）
    rows = [{"amount": Decimal("120.50")}, {"amount": Decimal("99.50")}]

    result = compressStepData(rows, maxRows=5)

    summary = result["columns"]["amount"]
    assert summary["max"] == 120.5
    assert summary["min"] == 99.5
    assert summary["avg"] == 110.0
    assert summary["sum"] == 220.0
    assert summary["top"][0]["amount"] == 120.5
    json.dumps(result)  # 不抛 ⇒ 可直接写进 JSONB 的 data_compressed


def testCompressNormalizesDbValuesIntoJsonNativeTypes():
    stamp = datetime(2025, 3, 1, 12, 30, 45)
    # created_at 不含 _TIME_HINT 的任一子串 ⇒ 走 category 的 distinct 分支
    rows = [
        {"created_at": stamp, "amount": Decimal("1.5")},
        {"created_at": stamp, "amount": Decimal("2.5")},
    ]

    result = compressStepData(rows, maxRows=5)

    assert result["rows"][0]["created_at"] == "2025-03-01T12:30:45"
    assert result["rows"][0]["amount"] == 1.5
    assert result["columns"]["created_at"]["distinct"] == ["2025-03-01T12:30:45"]
    json.dumps(result)


def testEstimatePromptTokensCountsCjkAndLatin():
    assert estimatePromptTokens("") == 0
    # 5 个汉字 ≈ 5 token；8 个 latin 字符 ≈ 2 token
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
from datetime import date, datetime
from decimal import Decimal

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

    kept = [_jsonSafeRow(row) for row in rows[:maxRows]]
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
        summary["top"] = [{c: _jsonSafe(row.get(c)) for c in columns} for row in ranked]
        return summary

    return {"distinct": _distinctSorted(present)[:MAX_DISTINCT_VALUES]}


def _distinctSorted(values: list) -> list:
    return sorted({_jsonSafe(v) for v in values if v is not None}, key=lambda v: str(v))


def _jsonSafeRow(row: dict) -> dict:
    """返回归一化后的新行（不改调用方的字典）。"""
    return {column: _jsonSafe(value) for column, value in row.items()}


def _jsonSafe(value: object) -> object:
    """把 DB 原值归一为 JSON 原生类型。

    `data_compressed` 是裸 JSONB 列（无 `default=str` 编码器），Decimal/datetime
    直接写入会抛 TypeError；而 `rows` / `top` / `distinct` 三处都会带出 DB 原值。
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        # datetime 是 date 的子类，两者共用 isoformat()。
        return value.isoformat()
    return value


def _isNumber(value: object) -> bool:
    if isinstance(value, bool):
        return False
    # 含 Decimal：业务库数值列常以 Decimal 返回
    # （同 data_summary._to_float / chat_multistep._summarizeStepData）。
    return isinstance(value, (int, float, Decimal))
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest app/tests/unit/test_multi_step_compressor.py -v
```
Expected: 13 passed（9 个测试函数 + `testShouldCompress` 4 个参数化用例）

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
  - `_openRun(self, session, *, sessionId, question, modelId, subQuestions, datasourceId=None) -> MultiStepRun | None`
  - `_persistStepSuccess(self, session, step, *, status, sql, data, chartOption, modelUsed, tokens, cost) -> None`
  - `_persistStepFailure(self, session, step, exc, *, run=None, tokens=0, cost=0) -> str`
    （`step` 可为 `None` ⇒ 只分类、不落库、仍返回 kind）
  - `_closeRun(self, session, run, *, status, completedSteps, currentStepIdx, errorSummary) -> None`
  - `_maybeCompressPriorSteps(self, session, run, steps, *, nextStepIdx, maxInputTokens, injectionText) -> bool`
- Produces（**模块级函数，不是方法**——Task 6 会 `from app.services.multi_step_persist_hooks import runStatusFor`，故必须建在本模块顶层）：
  - `runStatusFor(completed: int, total: int, anyFailed: bool, anySkipped: bool) -> str`（spec §4.2）

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
    # 必须带 id：_openRun 紧接着要把它喂给 createSteps(runId=run.id)
    run = SimpleNamespace(id="r1")
    createRun = AsyncMock(return_value=run)
    createSteps = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.createRun", createRun
    )
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.createSteps", createSteps
    )

    result = await host._openRun(
        session, sessionId="s", question="q", modelId=1, subQuestions=["a"]
    )

    assert result is run
    assert createRun.await_args.kwargs["totalSteps"] == 1
    assert createSteps.await_args.kwargs["runId"] == "r1"


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
    assert record.await_args.kwargs["tokens"] == 0
    assert record.await_args.kwargs["cost"] == 0
    assert updateRun.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def testPersistStepFailureWithStepNoneSkipsWriteButStillClassifies(monkeypatch):
    """kill switch 关掉时 step 为 None：只分类、不落库。

    缺这个守卫会让 recordStepError 在 `step.attempt_count` 抛 AttributeError，
    把原始的步错误顶掉 —— 关掉开关反而崩在守卫自身。传了 run 是为了同时钉住
    `if run is not None` 分支里的 `step.step_index` 访问也被早返回保护。
    """
    host = _Host()
    session = AsyncMock()
    record = AsyncMock()
    updateRun = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.recordStepError", record
    )
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.updateRun", updateRun
    )

    kind = await host._persistStepFailure(
        session, None, httpx.ConnectError("refused"),
        run=SimpleNamespace(status="running"),
    )

    assert kind == "transient"
    record.assert_not_awaited()
    updateRun.assert_not_awaited()


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


@pytest.mark.asyncio
async def testMaybeCompressCompressesPriorSucceededSteps(monkeypatch):
    host = _Host()
    session = AsyncMock()
    finishStep = AsyncMock()
    updateRun = AsyncMock()
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.finishStep", finishStep
    )
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.persistence.updateRun", updateRun
    )
    run = SimpleNamespace(id="r1", compressed_count=0)
    steps = [
        # 唯一符合「前于 nextStepIdx + succeeded + 有 data + 未压缩过」的步
        SimpleNamespace(
            step_index=0, status="succeeded",
            data=[{"a": i} for i in range(50)], data_compressed=None,
        ),
        SimpleNamespace(step_index=1, status="succeeded", data=None, data_compressed=None),
        SimpleNamespace(step_index=2, status="failed", data=[{"a": 1}], data_compressed=None),
        SimpleNamespace(step_index=3, status="succeeded", data=[{"a": 1}], data_compressed=None),
    ]

    # maxInputTokens=10 且注入文本远超阈值 ⇒ 必压
    changed = await host._maybeCompressPriorSteps(
        session, run, steps,
        nextStepIdx=3, maxInputTokens=10, injectionText="很长的注入文本" * 20,
    )

    assert changed is True
    # step 1 无 data、step 2 非成功态、step 3 不在 nextStepIdx 之前 ⇒ 只压 step 0
    assert finishStep.await_count == 1
    assert finishStep.await_args.args[1] is steps[0]
    assert finishStep.await_args.kwargs["status"] == "compressed"
    assert steps[0].data_compressed is not None
    assert steps[0].data_compressed["meta"]["original_rows"] == 50
    # 原始 data 永不被删除（spec §5：压缩结果另存 data_compressed）
    assert steps[0].data is not None and len(steps[0].data) == 50
    assert updateRun.await_args.kwargs["compressedCount"] == 1
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
    STEP_STATUS_COMPRESSED as _STEP_STATUS_COMPRESSED,
    STEP_STATUS_FAILED as _STEP_STATUS_FAILED,
    STEP_STATUS_SKIPPED as _STEP_STATUS_SKIPPED,
    STEP_STATUS_SUCCEEDED as _STEP_STATUS_SUCCEEDED,
    MultiStepRun,
    MultiStepStep,
)
from app.services import multi_step_persistence as persistence
from app.services.multi_step_compressor import (
    COMPRESS_THRESHOLD,
    compressStepData,
    estimatePromptTokens,
    shouldCompress,
)
from app.services.multi_step_retry import classifyStepError

logger = logging.getLogger(__name__)


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
        datasourceId: int | None = None,
    ) -> MultiStepRun | None:
        if not await self._isPersistEnabled(session):
            return None
        run = await persistence.createRun(
            session,
            sessionId=sessionId,
            question=question,
            modelId=modelId,
            datasourceId=datasourceId,
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
        tokens: int = 0,
        cost: float = 0,
    ) -> str:
        # spec §6.1：分类与落库解耦。kill switch 关掉时 `_openRun` 返回 None，
        # 调用方没有 step 行可传（只能传 None），但**仍然需要 kind** 去决定要不要
        # 重试 —— 故先分类，只在落库处短路。
        #
        # 缺这个守卫（`_persistStepSuccess` 早有同名守卫）会让 recordStepError 在
        # `step.attempt_count`（multi_step_persistence.py:114）抛 AttributeError，
        # 把原始的步错误顶掉：kill switch 一关，失败路径反而崩在守卫自身。
        kind = classifyStepError(exc)
        if step is None:
            return kind
        await persistence.recordStepError(
            session, step,
            message=f"{type(exc).__name__}: {exc}", kind=kind,
            tokens=tokens, cost=cost,
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
                        run.id, compressedCount, estimated, COMPRESS_THRESHOLD * 100, maxInputTokens)
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
- Modify: `backend/app/services/chat_multistep.py`（`_executeMultiStep`，加钩子调用 + 续跑分支）
- Modify: `backend/app/services/chat_stream.py`（`_streamMultiStep`，加钩子调用 + 续跑分支 + 两个新 SSE 字段/事件）
- Modify: `backend/app/services/stream_events.py`（新增 `EVENT_STEP_COMPRESSED` 常量）
- Modify: `backend/app/domain/schemas.py`（`ChatRequest` 加 `resumeRunId`，为 Task 7 的续跑载体）
- Modify: `backend/app/services/multi_step_persistence.py`（新增 `adoptRunForResume`）
- Test: `backend/app/tests/integration/test_multi_step_persist_wiring.py`

**Interfaces:**
- Consumes: Task 5 的钩子方法、现有 `_executeDataStep` / `StepExecutionContext.inject_to_prompt`、Task 2 的 `loadRun`/`loadSteps`/`resetStepsFrom`
- Produces（Task 9 依赖，全部在 SSE 层）:
  - `multi_step_plan` 事件的 data 增加 `runId: str | None`（单步路径为 `None`）
  - 新事件 `step_compressed`（`EVENT_STEP_COMPRESSED = "step_compressed"`），data 形如
    `{"stepIndex": int, "originalRows": int, "compressedRows": int}`
  - 其余行为变化是落库有副作用
- Produces（**Task 7 依赖**，非 SSE）:
  - `app.domain.schemas.ChatRequest` 新字段 `resumeRunId: str | None = None`
  - `multi_step_persistence.adoptRunForResume(session, *, runId, subQuestions) -> tuple[MultiStepRun | None, int]`

**改动纪律:** 除续跑分支外，只加「打开 run / 每步落库 / 关 run / 压缩判定」四类调用，**不得**改 NL2SQL 逻辑、不得引入模型改派。

**续跑模式（本任务与 Task 7 的接口契约，2026-10-05 人类裁决为「最小正确版」）：**

人类裁决原文：*「Task 6 增续跑模式：携带 runId 复用既有 run（不新建、不留僵尸），步循环从 start 起、跳过更早步。不做前序结果回灌、不复用已存 SQL。§7.2 的 plan 重放 + sql_hash 命中复用登记为遗留项。」*

背景（**必读，否则会写错**）：计划原先让路由把 `resumeFromStep=startIndex` 塞进 `ChatRequest`，但全仓 grep 证明**无任何代码消费它**；而本任务原定的 `_openRun` 是**无条件**的，于是续跑会新建**第二个** run，被 `prepareResume` 重置成 pending 的那个原 run 则**永远停在 running**（僵尸）。本节就是修掉这条链路：

- 载体改为 `ChatRequest.resumeRunId`（`str | None`）。**起始步不再由 DTO 传递**，唯一事实来源是 DB 的 `multi_step_run.current_step_idx`（Task 7 的 `prepareResume` 已写入）。
- 续跑路径**绝不调用 `_openRun`**，改用 `persistence.adoptRunForResume(...)`。
- 循环里 `index < startIndex` 的步**跳过执行**，但**必须计入 `completed`** —— 否则 `_closeRun` 的 `runStatusFor(completed, total, ...)` 会把「跳过的成功步」当成未完成，把 run 误判成 `failed`。
- **不做**前序步结果回灌（不进 prompt 上下文），**不复用**已存 SQL。依据：2026-09-28 真机诊断已证伪「拆步产生步间数据依赖」（见 memory `qa-system-multistep-no-data-dependency`），跳过更早步不损失正确性。

**本任务不管**「续跑被重新路由成单步」的封口 —— 那由 **Task 7 的路由在流结束后兜底**处理（见该任务的 `_sealAbandonedResume`），因为只有路由那层能覆盖「流中途断掉」等一切提前退出的形态。这两个文件里**不要**再加单步分支的守卫。


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
    # **必须用 test_chat_multi_step 的 fake**：多步拆解由「查询拆分器」这个 system
    # prompt 分支驱动，只有 `_MultiStepLlm` 实现了它。`test_chat_api._PipelineLlm`
    # 没有该分支（落到 else 回一句自然语言）⇒ 根本不会产生多步计划 ⇒ 本用例会
    # 因为「一条 run 都没有」而红，且原因极具误导性。
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _MultiStepLlm,
        _OkAdapter,
        _install,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    # 这个问句是 test_chat_multi_step 里已验证会走多步、且拆出 2 步的那个。
    question = "请分步查询 2024 和 2025 年的销售额并对比"

    # Act
    resp = await pg_client.post(
        "/api/v1/chat", json=_payload(question, datasource.id)
    )

    # Assert
    assert resp.status_code == 200
    runs = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.question == question))
    ).scalars().all()
    assert len(runs) == 1, "多步跑完必须恰好落 1 条 run"
    steps = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == runs[0].id).order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    assert [s.step_index for s in steps] == [0, 1]
    assert all(s.status == "succeeded" for s in steps)
    assert all(s.sql for s in steps)


@pytest.mark.asyncio
async def testRunMarkedFailedWhenStepExhaustsRetries(pg_client, db_session, monkeypatch):
    """第 2 步 LLM 持续 ConnectError → run.status=failed，第 1 步仍 succeeded。"""
    import httpx

    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _MultiStepLlm,
        _OkAdapter,
        _install,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    import app.services.llm_retry_policy as retryPolicy
    monkeypatch.setattr(retryPolicy, "RETRY_WAIT_MIN_SECONDS", 0)
    monkeypatch.setattr(retryPolicy, "RETRY_WAIT_MAX_SECONDS", 0)
    monkeypatch.setattr(
        "app.services.multi_step_retry.TRANSIENT_WAITS", (0, 0)
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

    question = "请分步查询 2024 和 2025 年的销售额并对比"
    resp = await pg_client.post("/api/v1/chat", json=_payload(question, datasource.id))

    assert resp.status_code in (200, 502, 503)
    run = (
        await db_session.execute(
            select(MultiStepRun).where(MultiStepRun.question == question)
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
    assert steps[-1].status == "failed"   # spec §6.3：瞬态耗尽 → 步终态 failed
    assert steps[-1].last_error_kind == "transient"


@pytest.mark.asyncio
async def testPersistDisabledWritesNoRows(pg_client, db_session, monkeypatch):
    """kill switch 关掉 ⇒ 不落库，但多步本身照跑，且 `runId` 为 None。

    走**流式**而不是非流式：只断言 `runs == []` 的话，一个「压根没走多步」的
    装配也能让它通过（假绿）。这里用流式的 `multi_step_plan` 事件反过来钉住
    「多步确实跑了、且拆出 2 步」，同时顺带钉住 Task 9 依赖的 `runId: None` 分支。
    """
    from types import SimpleNamespace

    from app.services.stream_events import EVENT_MULTI_STEP_PLAN
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _MultiStepLlm,
        _OkAdapter,
        _install,
        _parseFrames,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.getSettings",
        lambda: SimpleNamespace(multiStepPersistEnabled=False),
    )

    resp = await pg_client.post(
        "/api/v1/chat/stream",
        json=_payload("请分步查询 2024 和 2025 年的销售额并对比", datasource.id),
    )

    assert resp.status_code == 200
    overview = [d for e, d in _parseFrames(resp) if e == EVENT_MULTI_STEP_PLAN]
    assert len(overview[0]["steps"]) == 2
    assert overview[0]["runId"] is None, "开关关掉时没有 run，runId 必须是 None"

    runs = (await db_session.execute(select(MultiStepRun))).scalars().all()
    assert runs == []


@pytest.mark.asyncio
async def testStreamMultiStepPlanCarriesRunId(pg_client, db_session, monkeypatch):
    """流式 multi_step_plan 事件必须带 runId —— Task 9 前端续跑按钮的唯一来源。

    复用 test_chat_multi_step 的 fake 装配（同一个真实 API 链路）。断言分两层：
    键存在且是字符串，**并且**这个 id 真的能在库里查到 run —— 只断言字符串
    的话，随手 `str(uuid.uuid4())` 也能过。
    """
    from app.services.stream_events import EVENT_MULTI_STEP_PLAN
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _MultiStepLlm,
        _OkAdapter,
        _install,
        _parseFrames,
        _payload,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    # Act
    resp = await pg_client.post(
        "/api/v1/chat/stream",
        json=_payload("请分步查询 2024 和 2025 年的销售额并对比", datasource.id),
    )

    # Assert
    assert resp.status_code == 200, resp.text
    overview = [d for e, d in _parseFrames(resp) if e == EVENT_MULTI_STEP_PLAN]
    assert len(overview) == 1
    runId = overview[0]["runId"]
    assert isinstance(runId, str) and runId
    run = (
        await db_session.execute(
            select(MultiStepRun).where(MultiStepRun.id == uuid.UUID(runId))
        )
    ).scalar_one()
    assert run.question


@pytest.mark.asyncio
async def testStreamEmitsStepCompressedForEarlierStep(pg_client, db_session, monkeypatch):
    """压缩更早的步后补发 step_compressed —— Task 9「已压缩」徽章的唯一来源。"""
    from app.services.stream_events import EVENT_STEP_COMPRESSED
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _MultiStepLlm,
        _install,
        _parseFrames,
        _payload,
    )

    class _BigRowsAdapter:
        """固定返回 40 行：超过 DEFAULT_MAX_ROWS=30，让压缩比 != 1。

        行数必须真的超过保留上限，否则 originalRows == compressedRows，
        实现把两个数写反也照样通过。
        """

        def __init__(self) -> None:
            self.executed: list[str] = []

        async def execute_read_only(self, sql: str) -> list[dict]:
            self.executed.append(sql)
            return [{"NAME": f"N{i}", "QTY": i} for i in range(40)]

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _BigRowsAdapter())
    # 阈值恒真：只验证「压缩发生了 → 事件被发出来」，不依赖 token 估算的具体数值
    monkeypatch.setattr(
        "app.services.multi_step_persist_hooks.shouldCompress", lambda *a, **k: True
    )

    # Act
    resp = await pg_client.post(
        "/api/v1/chat/stream",
        json=_payload("请分步查询 2024 和 2025 年的销售额并对比", datasource.id),
    )

    # Assert
    assert resp.status_code == 200, resp.text
    compressed = [d for e, d in _parseFrames(resp) if e == EVENT_STEP_COMPRESSED]
    # 只在处理第 2 步前压一次；_maybeCompressPriorSteps 对已有 data_compressed 的步会跳过
    assert len(compressed) == 1
    assert compressed[0]["stepIndex"] == 0
    assert compressed[0]["originalRows"] == 40
    assert compressed[0]["compressedRows"] == 30

    # 事件数字必须与落库的压缩结果一致（口径只有一个来源）
    step0 = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.step_index == 0)
        )
    ).scalars().one()
    assert step0.status == "compressed"
    assert step0.data_compressed["meta"]["original_rows"] == 40


@pytest.mark.asyncio
async def testAdoptRunForResumeAlignsShapeAndStart(pg_client, db_session):
    """`adoptRunForResume` 三个分支：形状一致保留起点 / 变长 / 变短。

    这是续跑唯一「不新建 run」的入口（最小正确版裁决）。三个分支分别对应：
    正常续跑、`model_override` 换了模型后重新规划出更多步、重新规划出更少步。
    """
    from app.domain.research_models import ResearchSession
    from app.services import multi_step_persistence as repo
    from app.services.multi_step_persistence import adoptRunForResume

    sessionRow = ResearchSession(id=uuid.uuid4(), title="adopt-1", created_by=1)
    db_session.add(sessionRow)
    await db_session.commit()
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="q", modelId=1, totalSteps=2
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A", "查B"])
    await repo.finishStep(
        db_session, steps[0], status="succeeded", sql="SELECT 1", data=[{"a": 1}]
    )
    await repo.recordStepError(db_session, steps[1], message="timeout", kind="transient")
    await repo.updateRun(db_session, run, status="failed", completedSteps=1, currentStepIdx=1)
    await db_session.commit()

    # --- 分支 1：形状一致 ⇒ 保留 current_step_idx，且**不动**已成功的第 0 步 ---
    adopted, start = await adoptRunForResume(db_session, runId=run.id, subQuestions=["查A", "查B"])
    assert start == 1
    rows = await repo.loadSteps(db_session, run.id)
    assert rows[0].status == "succeeded", "已成功的更早步不能被重置（续跑就是靠它省掉重跑）"
    assert rows[0].sql == "SELECT 1", "更早步的 SQL 必须留着"
    assert rows[0].data == [{"a": 1}], "spec §5.3：data 永不删除"
    assert rows[1].status == "pending"
    assert rows[1].last_error is None
    assert rows[1].sql is None, "重跑会重新生成 SQL，留着旧的会污染将来的 sql_hash 复用"

    # --- 分支 2：计划变长（换了模型重新规划）⇒ 起点归零、补齐新行 ---
    adopted2, start2 = await adoptRunForResume(
        db_session, runId=run.id, subQuestions=["查X", "查Y", "查Z"]
    )
    assert start2 == 0, "形状变了，旧的「已完成」对应的是别的子问题，不能跳过任何步"
    assert adopted2.total_steps == 3
    rows = await repo.loadSteps(db_session, run.id)
    assert [s.step_index for s in rows] == [0, 1, 2]
    assert [s.sub_question for s in rows] == ["查X", "查Y", "查Z"]
    assert all(s.status == "pending" for s in rows)
    assert rows[0].sql is None, "形状变了 ⇒ 全跑，旧 SQL 必须清掉"

    # --- 分支 3：计划变短 ⇒ 删掉多余尾行（否则 stepsByIdx 里会留下对不上的孤儿） ---
    _adopted3, start3 = await adoptRunForResume(db_session, runId=run.id, subQuestions=["查X"])
    assert start3 == 0
    rows = await repo.loadSteps(db_session, run.id)
    assert [s.step_index for s in rows] == [0]
    assert rows[0].sub_question == "查X"

    # --- run 不存在（并发删除）⇒ (None, 0)，调用方回退普通路径，不炸 ---
    gone, start4 = await adoptRunForResume(
        db_session, runId=uuid.uuid4(), subQuestions=["查A"]
    )
    assert gone is None and start4 == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
export TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test
export TEST_NEO4J_URI=bolt://localhost:7688
pytest app/tests/integration/test_multi_step_persist_wiring.py -v
```
Expected: `testMultiStepRunPersistedEndToEnd` FAIL（`assert len(runs) == 1` 得到 0）；`testStreamMultiStepPlanCarriesRunId` FAIL（`KeyError: 'runId'`）；`testStreamEmitsStepCompressedForEarlierStep` FAIL（`assert len(compressed) == 1` 得到 0）；`testPersistDisabledWritesNoRows` FAIL（`runId` 键还不存在）；`testAdoptRunForResumeAlignsShapeAndStart` FAIL（`ImportError: cannot import name 'adoptRunForResume'`）。

- [ ] **Step 3: 加 `adoptRunForResume`（仓储层，续跑的形状对齐）**

`backend/app/services/multi_step_persistence.py` 末尾追加（`logger` 若模块里还没有，在 import 区补 `import logging` + `logger = logging.getLogger(__name__)`）：

```python
async def adoptRunForResume(
    session: AsyncSession,
    *,
    runId: uuid.UUID,
    subQuestions: list[str],
) -> tuple[MultiStepRun | None, int]:
    """续跑：复用既有 run 并把它的步行对齐到**本次**计划。返回 (run, 起始步号)。

    调用方（Task 6 的接线）**绝不**再调 `createRun` —— 那会新建第二个 run，而
    被 `prepareResume` 重置过的原 run 会永远停在 running（僵尸）。

    起始步的唯一事实来源是 `run.current_step_idx`（Task 7 的 `prepareResume` 写入）。
    只有当**计划的形状与原 run 逐字一致**时才沿用它；形状变了（`model_override`
    换了模型、或重新规划出不同的子问题）时归零整跑，因为旧的「已完成」对应的
    是别的子问题，跳过它们就是跑错。

    形状对齐同时负责行数：变长补行、变短删尾（否则 `stepsByIdx[index]` 会 KeyError
    或留下对不上的孤儿行）。

    重置范围是 `>= start`：更早的已成功步**原样保留**（`sql` / `data` 都不动）——
    续跑省掉重跑正是靠它。`data` 按 spec §5.3 永不删除；`sql` / `sql_hash` 清掉是
    刻意的：既不复用旧 SQL，也不给将来「sql_hash 命中即复用」的遗留项留一个会
    误命中的陈旧哈希。
    """
    run = await loadRun(session, runId)
    if run is None:
        return None, 0
    steps = await loadSteps(session, runId)
    shapesMatch = [s.sub_question for s in steps] == list(subQuestions)
    start = int(run.current_step_idx or 0) if shapesMatch else 0
    if not shapesMatch:
        logger.info(
            "续跑形状变化：run=%s 原 %d 步 → 本次 %d 步，起始步归零",
            run.id, len(steps), len(subQuestions),
        )

    byIndex = {s.step_index: s for s in steps}
    for surplus in steps:
        if surplus.step_index >= len(subQuestions):
            await session.delete(surplus)
    for index, text in enumerate(subQuestions):
        step = byIndex.get(index)
        if step is None:
            session.add(MultiStepStep(
                id=uuid.uuid4(), run_id=runId, step_index=index,
                status=STEP_STATUS_PENDING, sub_question=text,
            ))
            continue
        step.sub_question = text
        if index >= start:
            step.status = STEP_STATUS_PENDING
            step.last_error = None
            step.last_error_kind = None
            step.attempt_count = 0
            step.finished_at = None
            step.data_compressed = None
            step.sql = None
            step.sql_hash = None
    run.total_steps = len(subQuestions)
    run.current_step_idx = start
    await session.flush()
    return run, start
```

- [ ] **Step 4: 在 `_executeMultiStep` 接线**

先读现状：
```bash
sed -n '557,600p' backend/app/services/chat_multistep.py
sed -n '690,745p' backend/app/services/chat_multistep.py
```
在 steps 循环**之前**插入：
```python
        subQuestions = [s.description or s.subQuestion for s in multiStepPlan.steps]
        # 续跑模式（Task 7 传 resumeRunId）：复用既有 run，**绝不**新建。
        # 新建会让 prepareResume 重置过的那个 run 永远停在 running（僵尸），
        # 且落库落在一个与用户所见无关的新 run 上。
        resumeRunId = getattr(dto, "resumeRunId", None)
        run = None
        startIndex = 0
        if resumeRunId:
            run, startIndex = await persistence.adoptRunForResume(
                session, runId=uuid.UUID(str(resumeRunId)), subQuestions=subQuestions,
            )
            # adopt 返回 None 只在并发删除时发生（路由已 404 过）。此时退回普通
            # 新建路径，宁可多一条 run，也不能因为续跑而整轮失败。
        if run is None:
            run = await self._openRun(
                session,
                sessionId=dto.sessionId,
                question=dto.question,
                modelId=getattr(dto, "modelId", None),
                subQuestions=subQuestions,
                datasourceId=getattr(dto, "datasourceId", None),
            )
            startIndex = 0
```
（字段名以实际 `multiStepPlan.steps` 的元素属性为准；若为 `StepPlan` 用 `s.sub_question`）
`uuid` 若未 import，在 import 区补 `import uuid`。

**同时**在 `backend/app/domain/schemas.py` 的 `ChatRequest`（实测在 `domain/schemas.py:1647`，
不是 `models/schemas.py` —— 后者不存在）上加一个可选字段，声明风格与相邻字段一致（该类的
字段是**直接写成 camelCase** 的，如 `sessionId` / `datasourceId` / `modelId`）：

```python
    # 续跑（spec §7）：非空时执行链路复用这个 run 而不是新建（Task 7 的路由填）。
    # 起始步不从这里传 —— 唯一事实来源是 multi_step_run.current_step_idx。
    resumeRunId: str | None = Field(default=None, description="续跑：复用既有的 multi-step run")
```

`CamelModel` 未设 `extra=forbid`，加字段是纯增量、不影响既有入参形状（spec §10.3 要求
不修改 `/api/v1/chat` 入参形状 —— 加一个默认 `None` 的可选键满足该要求）。

**循环头必须改成带下标**：现状是 `for step_plan in multiStepPlan.steps:`（`chat_multistep.py:622`），
没有 `index`。本步骤下面所有 `stepsByIdx[index]` / 跳过分支都依赖它，故改成
```python
        for index, step_plan in enumerate(multiStepPlan.steps):
```

在循环体内、**取 `stepsByIdx[index]` 之前**插入跳过分支：
```python
            if index < startIndex:
                # 续跑：更早的步已经 succeeded，跳过重跑。**必须**照样计入完成数，
                # 否则 _closeRun 的 runStatusFor 会把「跳过的成功步」当未完成 ⇒
                # run 被误判 failed，用户看到续跑「又失败了」。
                # 前序步结果**不**回灌进 prompt（2026-09-28 诊断已证伪拆步产生步间
                # 数据依赖，见 memory qa-system-multistep-no-data-dependency）。
                completedCount += 1
                continue
```
`completedCount` / `anyFailed` / `anySkipped` 的初始化（下面提到的那三行）必须仍在**这个**跳过分支之前。

> **命名警告（必读）**：本方法里**已经有一个** `completed: list[StepResult]`（`chat_multistep.py:613`），
> 被 `_hasDataStepResult(completed)` 与 `_multiStepResponse(completed=completed, ...)` 使用。
> **绝对不要**把它复用成计数器 —— 那会静默改掉汇总与读模型的入参类型。
> 计数器一律用 `completedCount: int`。流式版（Step 5）同理，先 grep 该方法的 `completed` 再动手。

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
                await self._persistStepFailure(
                    session, stepsByIdx[index], exc, run=run,
                    tokens=getattr(exc, "tokens_used", 0),
                    cost=getattr(exc, "cost_used", 0.0),
                )
                # 落步的终态（spec §6.3 永久错误 → failed；§4.1 的 skip 分支 → skipped）。
                # _persistStepFailure 只把步置为 running（per-attempt 语义），终态在此落一次。
                await persistence.finishStep(
                    session, stepsByIdx[index],
                    status=STEP_STATUS_SKIPPED if isSkip else STEP_STATUS_FAILED,
                )
                if isSkip:
                    anySkipped = True
                else:
                    anyFailed = True
```
循环**之后**：
```python
        await self._closeRun(
            session, run,
            status=runStatusFor(
                completedCount, len(multiStepPlan.steps), anyFailed, anySkipped
            ),
            completedSteps=completedCount,
            # run 已收尾，指针挪到末尾；resume 用的是 Task 7 prepareResume 另写的值，
            # 不读这里。
            currentStepIdx=len(multiStepPlan.steps),
        )
        await session.commit()
```
`completedCount` / `anyFailed` / `anySkipped` 由本步骤自行维护：循环开始前置
`completedCount = 0; anyFailed = False; anySkipped = False`；每步成功后 `completedCount += 1`。
`except` 分支里先算出 `isSkip`（该步是否走 spec §4.1 的 `skipped` 分支——压缩后仍超限、后续不再补；不满足就是普通失败），再按它分别置 `anySkipped` / `anyFailed`——上面那段落步终态用的是同一个 `isSkip`。**不要**动上面那个 `completed` 列表——它是 `_hasDataStepResult` / `_multiStepResponse` 的入参，与计数器是两回事。

**提前 `return` 的汇总分支也必须封口（漏了就是 `running` 僵尸）：**

`_executeMultiStep` 的汇总分支（`if step_plan.aggregation_only:` 的成功路径，
`chat_multistep.py:691` 的 `return _multiStepResponse(...)`）是**在循环体内直接 return**，
**走不到**循环之后那段 `_closeRun`。计划含汇总步时（线上常态），run 会永远停在 `running`。
测试用的 `_MULTI_STEP_PLAN_JSON` **没有** `aggregationOnly` 步 ⇒ **没有任何用例会发现这个漏**，
必须靠这里的指令补上。在该 `return` **之前**插入：
```python
                # 汇总步本身也算「跑完了」，不 +1 的话 completedCount 永远 <
                # len(steps)（分母含汇总步）⇒ runStatusFor 把成功的 run 判成 failed。
                completedCount += 1
                # _closeRun 自己就 `if run is None: return`（kill switch 关掉时 run=None），
                # 不需要外面再包一层判断。
                await self._closeRun(
                    session, run,
                    status=runStatusFor(
                        completedCount, len(multiStepPlan.steps), anyFailed, anySkipped
                    ),
                    completedSteps=completedCount,
                    currentStepIdx=len(multiStepPlan.steps),
                )
                await session.commit()
```
（注意：同一分支里「所有数据步都失败 ⇒ `continue`」那条路径**不要**加 —— 它不 return，
会落到循环之后的统一 `_closeRun`，那时 `anyFailed` 已是 True、终态自然是 failed。
在它里面再加一次会重复封口。）

流式版（Step 5）**同样**：`chat_stream.py:983` 的 `return` 也要在它上面插这一段
（`_closeRun` 之后 `await session.commit()` 的时机保持一致）。
`stepsByIdx` 的取法：`_openRun` 后立刻
```python
        persisted = await persistence.loadSteps(session, run.id) if run is not None else []
        stepsByIdx = {s.step_index: s for s in persisted}
```
并在 import 区加
`from app.domain.multi_step_models import STEP_STATUS_FAILED, STEP_STATUS_SKIPPED`、
`from app.services import multi_step_persistence as persistence` 与
`from app.services.multi_step_persist_hooks import runStatusFor`。

**同时**把每步的 LLM 调用包进瞬态重试：把 `_executeDataStep(...)` 的调用点改为
```python
            stepRun, attempts = await runWithTransientRetry(
                lambda: self._executeDataStep(...原有参数...),
                onError=lambda exc, attempt: self._persistStepFailure(
                    session, stepsByIdx[index], exc, run=run,
                    tokens=getattr(exc, "tokens_used", 0),
                    cost=getattr(exc, "cost_used", 0.0),
                )
                if run is not None else _noop(),
            )
```
若原调用点参数复杂，退而求其次：保留原调用，只在异常分支接 `_persistStepFailure`（自动重试仍生效，因为 `_executeDataStep` 内部的 `_runQueryWithRetry` 已有瞬态重试）。**二选一并在此步骤的注释里写明选了哪个。**

**失败尝试的用量来源（spec §6.2「每次重试 tokens_used / cost 累加」）：** `_executeDataStep`
内部已把各段 LLM / SQL 用量累加进局部 `tokens` / `cost`（见 `chat_multistep.py:329-392`），
但只在成功返回时交回；一旦抛出，这两笔就丢了，而成败与否正是 `onError` 要记的。为让
`onError` 能记上，在 `_executeDataStep` 的**每个失败出口**（`raise` 之前）把当下已累计的用量
挂到异常上：
```python
        exc.tokens_used = tokens
        exc.cost_used = float(cost)   # cost 可能是 Decimal，统一成 float 便于跨层传递
        raise
```
只加这两行赋值，**不改**任何 NL2SQL 生成 / SQL 校验 / 重试逻辑。若异常来自更靠前的阶段
（还没产生任何用量），异常上没有这两个属性，`getattr` 兜底为 0 —— 这是**正确**的：那时
确实没有可计费响应，记 0 不是漏记。**拿不到用量就记 0，禁止为了凑数传假值。**

- [ ] **Step 5: 在 `_streamMultiStep` 接线（同一套钩子 + 同一套续跑分支）**

```bash
grep -n "_streamMultiStep\|_executeDataStep\|MultiStepPlan\|inject_to_prompt" backend/app/services/chat_stream.py | head -30
```
在流式版循环里加与 Step 4 相同的 `_openRun` / `markStepRunning` / `_persistStepSuccess` / `_persistStepFailure` / `_closeRun`，**以及同一个续跑分支**（`resumeRunId` ⇒ `persistence.adoptRunForResume`；`run is None` 时才 `_openRun`）与**同一个跳过分支**（`index < startIndex ⇒ completedCount += 1; continue`）。

现成的落点（已核实，不必再猜）：`chat_stream.py:881` 是 `for step_plan in multiStepPlan.steps:`，
**同一个改动**——改成 `for index, step_plan in enumerate(multiStepPlan.steps):`；
`chat_stream.py:860` 已有 `completed: list[StepResult] = []`，同 Step 4 的命名警告：
计数器一律叫 `completedCount: int`，**绝不**复用那个列表。

**两条路径都要接**——本项目第七次踩「改多步只接了一条路径」（见 memory `qa-system-multistep-failure-isolation`）。
非流式（`_executeMultiStep`）与流式（`_streamMultiStep`）是两份循环，改一份漏一份不会被任何测试发现：Task 7 的续跑端点走的是**流式**，非流式的续跑只在测试里被直接调用。

**顺序要求（Task 9 依赖）：** run 的取得（`adoptRunForResume` 或 `_openRun`）必须排在**下发计划概览之前**。现状是
`chat_stream.py:869` 在循环前 `yield StreamEvent(EVENT_MULTI_STEP_PLAN, {"steps": [...]})`，
取得 run 那段要插在它**上面**（不是下面）。顺序反了 `run` 还是 None，下面那行就永远发不出 runId。

**`multi_step_plan` 事件增加 `runId`（Task 9 前端续跑按钮的唯一来源）：**

```python
        yield StreamEvent(EVENT_MULTI_STEP_PLAN, {
            # Task 9：前端凭 runId 调 POST /chat/multi-step/{runId}/resume。
            # 单步路径（_singleStepOverview）不落库、没有 run，故意不带这个键；
            # 前端必须按「可选」处理，缺省时不渲染续跑按钮。
            "runId": str(run.id) if run is not None else None,
            "steps": [
                {
                    "stepIndex": s.index,
                    "description": s.description,
                    "subQuestion": s.sub_question,
                    "aggregationOnly": s.aggregation_only,
                }
                for s in multiStepPlan.steps
            ],
        })
```

只加这一个键，**不改** `steps` 数组里任何字段（前端 `isStepPlanOverviewItem` 是白名单收窄，
多一个顶层键不影响既有断言）。`chat_stream.py:1057` 的 `_singleStepOverview` **保持原样**
（单步不落库，加 `None` 会让前端多一个恒为假的分支）。

非流式 `/chat`（`_executeMultiStep`）本次**不同步加** runId：其 `steps` 负载由
`chat_multistep.py` 的读模型另行构造，改动面超出本任务。后果是非流式渲染下没有续跑按钮，
已登记进 Task 10 的遗留项。

压缩判定插在 `inject_to_prompt` 调用**之前**：
```python
        injectionText = stepContext.inject_to_prompt(index)
        # 压缩会把**更早的**步置为 compressed（其 step_result 早就发过了），
        # 前端无从得知 —— 故这里在调用前后对比 data_compressed，为每个**新**
        # 被压缩的步补发一条 step_compressed（Task 9 的「已压缩」徽章靠它）。
        compressedBefore = {
            s.step_index: s.data_compressed for s in steps
        }
        await self._maybeCompressPriorSteps(
            session, run, steps, nextStepIdx=index,
            maxInputTokens=_maxInputTokens(pc), injectionText=injectionText,
        )
        for s in steps:
            compressed = s.data_compressed
            if compressed is None or compressedBefore.get(s.step_index) is not None:
                continue
            meta = compressed.get("meta") or {}
            yield StreamEvent(EVENT_STEP_COMPRESSED, {
                "stepIndex": s.step_index,
                "originalRows": int(meta.get("original_rows") or 0),
                "compressedRows": int(meta.get("compressed_rows") or 0),
            })
```
`step_compressed` 是**流式专属**事件：非流式路径没有增量推送通道，前端在非流式渲染下
看不到压缩徽章（已与 runId 一起登记进 Task 10 的遗留项）。

非流式（Step 3）只插入 `_maybeCompressPriorSteps(...)` 调用本身，**不要**插入
`compressedBefore` / `yield` 这两段（那里没有 SSE 通道，`yield` 会直接语法错误）。

新事件常量加在 `stream_events.py` 现有常量区（紧跟 `EVENT_STEP_RESULT` 之后）：
```python
EVENT_STEP_COMPRESSED = "step_compressed"  # 多步：某个**更早**的步被上下文压缩（Task 9 徽章）
```
`chat_stream.py` 的 import 区（`EVENT_MULTI_STEP_PLAN` 那一组，约 51 行）补上
`EVENT_STEP_COMPRESSED`。
`_maxInputTokens(pc)` 用一行 helper 取当前 model 配置的上限（找不到时返回 `0`，`shouldCompress` 会安全地返回 False）：
```python
def _maxInputTokens(pipelineContext) -> int:
    for cfg in getattr(pipelineContext, "configs", ()) or ():
        if getattr(cfg, "selected", False):
            return int(getattr(cfg, "max_input_tokens", 0) or 0)
    return 0
```

- [ ] **Step 6: 跑测试确认通过**

```bash
pytest app/tests/integration/test_multi_step_persist_wiring.py -v
```
Expected: 6 passed

（本文件的用例：`testMultiStepRunPersistedEndToEnd`、`testRunMarkedFailedWhenStepExhaustsRetries`、`testPersistDisabledWritesNoRows`、`testStreamMultiStepPlanCarriesRunId`、`testStreamEmitsStepCompressedForEarlierStep`、`testAdoptRunForResumeAlignsShapeAndStart`。）

- [ ] **Step 7: 回归既有 chat 套件**

```bash
pytest app/tests/integration/test_chat_multi_step.py app/tests/integration/test_multistep_global_filter.py -v
```
Expected: 与基线一致（无新增红）。若出现红，先判断是否为本计划引入，**不要**顺手改无关测试。

- [ ] **Step 8: 确认行数未超限**

```bash
wc -l backend/app/services/chat_multistep.py backend/app/services/chat_stream.py backend/app/services/multi_step_persist_hooks.py
```
Expected: `chat_multistep.py` < 1000。若逼近，把 Step 4/5 的重复段落抽成 mixin 方法。

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/chat_multistep.py backend/app/services/chat_stream.py \
        backend/app/services/stream_events.py backend/app/services/multi_step_persistence.py \
        backend/app/domain/schemas.py \
        backend/app/tests/integration/test_multi_step_persist_wiring.py
git commit -m "feat(multi-step): 执行链路接入落库/重试/压缩钩子，SSE 下发 runId 与 step_compressed，支持续跑复用 run"
```

---

### Task 7: 续跑 API

**Files:**
- Create: `backend/app/services/multi_step_resume.py`
- Modify: `backend/app/api/v1/chat.py`（新增路由）
- Test: `backend/app/tests/integration/test_multi_step_resume_api.py`

**Interfaces:**
- Consumes: Task 2 仓储、Task 5 钩子、**Task 6 的 `ChatRequest.resumeRunId` 与 `persistence.adoptRunForResume`**、现有 `assertSessionOwnership` / `getCurrentUser` / `_service.processMessageStream`
- Produces:
  - `prepareResume(session, *, runId, fromStepIndex, idempotencyKey) -> tuple[MultiStepRun, int]`
  - HTTP：`POST /api/v1/chat/multi-step/{runId}/resume`

**「起始步」的载体（2026-10-05 裁决后定死，别改回去）：** 路由**不**通过 DTO 传递起始步。
`prepareResume` 把它写进 `multi_step_run.current_step_idx`，Task 6 的接线再从那里读。
计划早期版本写的是 `ChatRequest(resumeFromStep=startIndex)` —— 全仓 grep 证明那个字段
**没有任何消费者**，照旧写会得到一个新建的第二个 run + 一个永远 `running` 的僵尸 run。

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
async def testResumeAdoptsExistingRunAndSkipsSucceededStep(pg_client, db_session, monkeypatch):
    """续跑走通 + **不新建第二个 run** + 跳过的成功步仍被算作已完成。

    这条用例是「最小正确版」裁决的回归闸，三个断言各堵一个真实缺陷：
    1. `len(allRuns) == 1` —— 原计划会把 resume 变成一次全新的 run（僵尸 + 重复）。
    2. `reloadedRun.status == "succeeded"` —— Task 6 的跳过分支若忘了把跳过的
       成功步计入 `completed`，`runStatusFor` 会把 run 判成 `failed`（用户看到
       「续跑又失败了」），而 `resume_count >= 1` 之类的弱断言完全发现不了。
    3. `steps[0].sql` 仍是原值 —— 跳过分支若漏了，第 0 步会被重跑并覆盖 SQL。
    """
    import json

    from app.domain.research_models import ResearchSession
    from app.services import multi_step_persistence as repo
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _MULTI_STEP_PLAN_JSON,
        _MultiStepLlm,
        _OkAdapter,
        _install,
    )

    config, datasource = await _seed(db_session)
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

    # 播种的 sub_question **必须**与 Task 6 会算出的 subQuestions 逐字一致：Task 6 的
    # adoptRunForResume 以 sub_question 逐字相等判定「形状未变」，形状一变得归零整跑，
    # 本条用例的「跳过」断言就失效了。故这里**从同一个 `_MULTI_STEP_PLAN_JSON` 反推**，
    # 而不是手抄字符串 —— 注意 Task 6 的取值是 `description or subQuestion`（描述优先），
    # 手抄成 subQuestion 会静默对不上。
    question = "请分步查询 2024 和 2025 年的销售额并对比"
    planSteps = json.loads(_MULTI_STEP_PLAN_JSON)["steps"]
    subQuestions = [s.get("description") or s["subQuestion"] for s in planSteps]
    assert len(subQuestions) == 2

    sessionRow = ResearchSession(id=uuid.uuid4(), title="resume-2", created_by=1)
    db_session.add(sessionRow)
    await db_session.commit()
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question=question,
        modelId=config.id, datasourceId=datasource.id, totalSteps=2,
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=subQuestions)
    await repo.finishStep(
        db_session, steps[0], status="succeeded",
        sql="SELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT GROUP BY NAME",
        data=[{"NAME": "A", "QTY": 10}],
    )
    await repo.recordStepError(db_session, steps[1], message="timeout", kind="transient")
    await repo.updateRun(
        db_session, run, status="failed", completedSteps=1, currentStepIdx=1, finished=True,
    )
    await db_session.commit()

    # Act
    resp = await pg_client.post(
        f"/api/v1/chat/multi-step/{run.id}/resume",
        json={"from_step_index": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    # Assert
    assert resp.status_code == 200, resp.text
    # ① 全程只有这一条 run（resume 复用而非新建）
    allRuns = (await db_session.execute(select(MultiStepRun))).scalars().all()
    assert len(allRuns) == 1, f"续跑不得新建 run，实际 {len(allRuns)} 条"
    assert allRuns[0].id == run.id

    reloaded = (
        await db_session.execute(
            select(MultiStepStep).where(MultiStepStep.run_id == run.id).order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    assert reloaded[0].status == "succeeded"
    assert reloaded[0].data == [{"NAME": "A", "QTY": 10}], "跳过的成功步不得被重跑覆盖"
    assert reloaded[1].last_error is None
    assert reloaded[1].status == "succeeded", "第 2 步应在续跑里跑成功"

    reloadedRun = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.id == run.id))
    ).scalar_one()
    await db_session.refresh(reloadedRun)
    assert reloadedRun.resume_count >= 1
    # ② 跳过的成功步计入 completed ⇒ 终态 succeeded（漏计会得到 failed）
    assert reloadedRun.status == "succeeded"
    assert reloadedRun.finished_at is not None, "续跑跑完必须封口，不能留下 running 僵尸"


@pytest.mark.asyncio
async def testPrepareResumeClearsStaleCompressedPayload(db_session):
    """从压缩步续跑必须清掉 data_compressed，否则留下「status=pending 但
    data_compressed 非空」的非法态（spec §5.3），且压缩钩子见非空即跳过
    ⇒ 该步此后永远无法再压缩。"""
    from app.domain.research_models import ResearchSession
    from app.services import multi_step_persistence as repo
    from app.services.multi_step_resume import prepareResume

    sessionRow = ResearchSession(id=uuid.uuid4(), title="resume-compressed", created_by=1)
    db_session.add(sessionRow)
    await db_session.commit()
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question="q", modelId=1, totalSteps=2
    )
    steps = await repo.createSteps(db_session, runId=run.id, subQuestions=["查A", "查B"])
    await repo.finishStep(db_session, steps[0], status="succeeded", data=[{"a": 1}])
    await repo.finishStep(db_session, steps[1], status="compressed", data=[{"b": 1}])
    steps[1].data_compressed = {"rows": 1}
    await repo.updateRun(db_session, run, status="failed", completedSteps=1, finished=True)
    await db_session.commit()

    # Act
    _run, start = await prepareResume(
        db_session, runId=run.id, fromStepIndex=1, idempotencyKey=None
    )

    # Assert
    assert start == 1
    reloaded = await repo.loadSteps(db_session, run.id)
    assert reloaded[1].status == "pending"
    assert reloaded[1].data_compressed is None
    assert reloaded[0].data_compressed is None  # 前序步不被动


@pytest.mark.asyncio
async def testResumeIsIdempotentOnSameKey(pg_client, db_session, monkeypatch):
    from app.domain.research_models import ResearchSession
    from app.services import multi_step_persistence as repo
    from app.tests.integration.test_chat_api import _seed
    from app.tests.integration.test_chat_multi_step import (
        _MultiStepLlm,
        _OkAdapter,
        _install,
    )

    config, datasource = await _seed(db_session)
    # 必须装多步 fake：第一次续跑会真的把流跑完，问句也得是多步问句，
    # 否则多步链路不进，run 无人封口（靠 Task 7 的 _sealAbandonedResume 兜底，
    # 但那条路径不该是本用例要验的幂等语义）。
    _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())
    question = "请分步查询 2024 和 2025 年的销售额并对比"

    sessionRow = ResearchSession(id=uuid.uuid4(), title="resume-3", created_by=1)
    db_session.add(sessionRow)
    await db_session.commit()
    run = await repo.createRun(
        db_session, sessionId=sessionRow.id, question=question,
        modelId=config.id, datasourceId=datasource.id, totalSteps=1,
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
    # 幂等的实证：同 key 第二次请求不得再抬 resume_count
    reloadedRun = (
        await db_session.execute(select(MultiStepRun).where(MultiStepRun.id == run.id))
    ).scalar_one()
    await db_session.refresh(reloadedRun)
    assert reloadedRun.resume_count == 1
    assert key in (reloadedRun.idempotency_keys or [])


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
    # spec §5.3：data_compressed 仅当 status=compressed 时有值。resetStepsFrom 只回退
    # 状态、不清 data_compressed，故这里显式清空被重置范围，否则会留下
    # 「status=pending 但 data_compressed 非空」的非法态，且压缩钩子见非空即跳过
    # （_maybeCompressPriorSteps）⇒ 该步此后永远无法再压缩。
    for step in steps:
        if step.step_index >= start:
            step.data_compressed = None
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

**先确认 DTO 命名约定**（否则前后端对不上会 422）。**注意实际路径**——`app/models/schemas.py`
**不存在**，`ChatRequest` 实测在 `app/domain/schemas.py:1647`：
```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
grep -n "class ChatRequest" -A 25 app/domain/schemas.py
```
实测结论（已核实，不必再猜）：`ChatRequest(CamelModel)`，其字段是**直接写成 camelCase**
（`sessionId` / `datasourceId` / `modelId`），不是 snake_case + 别名生成器。故 `ResumeRequest`
也用 `CamelModel`，字段名写成 camelCase 风格与 `ChatRequest` 对齐 —— 但**本计划的接口契约是
snake_case 的 `from_step_index`**（spec §7.1 如此规定，且 Task 9 的前端发的是
`{"fromStepIndex": …}`，`CamelModel` 的 `to_camel` 会把 `from_step_index` 映射成 `fromStepIndex`，
`populate_by_name=True` 让两种写法都能进）。**两边只能选一种写死**：保留 `from_step_index`，
并在 Task 9 侧发 `fromStepIndex`。

`backend/app/api/v1/chat.py`，在 `suggestQueries` 之后追加：
```python
class ResumeRequest(CamelModel):
    from_step_index: int | None = None
    model_override: int | None = None
    compress_again: bool = False


async def _sealAbandonedResume(session: AsyncSession, runId: uuid.UUID) -> None:
    """续跑兜底封口：流跑完后 run 仍是 running ⇒ 没人关它，显式标失败。

    为什么需要：路由只给出 `run.question`，**重新路由的结果不一定还是多步**
    （首步这次成功了 ⇒ 单步优先策略不拆步），多步链路根本没进入，`adoptRunForResume`
    也就没被执行；也可能流中途断掉。两种情况下这条 run 都会永远停在 `running`。
    放在路由层是因为它是唯一能覆盖「一切提前退出形态」的位置。
    """
    refreshed = await multi_step_persistence.loadRun(session, runId)
    if refreshed is None or refreshed.status != RUN_STATUS_RUNNING:
        return
    await multi_step_persistence.updateRun(
        session, refreshed, status=RUN_STATUS_FAILED, finished=True,
        errorSummary="续跑未走多步链路（被重新路由为单步或流中断），run 已显式封口",
    )
    await session.commit()


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

    if run.datasource_id is None:
        # ChatRequest.datasourceId 是必填 int；缺了它只能 500，不如显式 409。
        raise ConflictError("该 multi-step run 没有数据源快照，无法续跑")

    idempotencyKey = request.headers.get("Idempotency-Key")
    try:
        await multi_step_resume.prepareResume(
            session, runId=runId, fromStepIndex=dto.from_step_index,
            idempotencyKey=idempotencyKey,
        )
    except multi_step_resume.ResumeNotAllowed as exc:
        # 上面已查过一次 run；这里兜的是查完与被删之间的竞态。不兜就是一个
        # 未捕获的领域异常 ⇒ 500，而正确答案是 404。
        raise NotFoundError(str(exc)) from exc
    except multi_step_resume.ResumeConflict as exc:
        raise ConflictError(str(exc)) from exc

    # 起始步**不**通过 DTO 传递：prepareResume 已把它写进 run.current_step_idx，
    # Task 6 的 adoptRunForResume 从那里读。唯一事实来源 = DB。
    chatDto = ChatRequest(
        question=run.question,
        sessionId=str(run.session_id),
        datasourceId=run.datasource_id,
        modelId=dto.model_override or run.model_id,
        resumeRunId=str(run.id),
    )

    async def eventSource() -> AsyncIterator[str]:
        try:
            async for event in _service.processMessageStream(chatDto, session, user=_user):
                yield event.toSse()
        finally:
            # finally 而非「循环后」：客户端断连时生成器被取消，CancelledError 也会
            # 走到这里，run 照样被封口（H4 断连落库那一课）。
            await _sealAbandonedResume(session, runId)

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
from app.domain.multi_step_models import RUN_STATUS_FAILED, RUN_STATUS_RUNNING
from app.domain.schemas import ChatRequest  # 实测路径：domain/schemas.py:1647
from app.services import multi_step_persistence, multi_step_resume
```

- [ ] **Step 5: 确认 `datasource_id` 快照已接上**

`datasource_id` 列在 Task 1 建好、`createRun` 已支持 `datasourceId`、`_openRun` 与
Task 6 的接线也已透传（Task 6 Step 4 的 `_openRun(... datasourceId=getattr(dto, "datasourceId", None))`）。
本步只做**核对**（若 Task 6 漏了，补上）：
```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
grep -n "datasourceId" app/services/multi_step_persist_hooks.py app/services/chat_multistep.py app/services/chat_stream.py
```
Expected: `_openRun` 的签名与两处调用点都能看到 `datasourceId`；`createRun` 收到它。
没有这一列，Task 7 的路由就拼不出合法的 `ChatRequest`（`datasourceId` 必填）。

- [ ] **Step 6: 跑测试确认通过**

```bash
pytest app/tests/integration/test_multi_step_resume_api.py -v
```
Expected: 5 passed

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
- Create: `backend/app/jobs/__init__.py`（新子包；本仓约定每个子包都有 `__init__.py`，
  现有 `app/api` `app/domain` `app/infrastructure` `app/models` `app/schemas` `app/services`
  `app/tests` `app/utils` `app/workers` 9 个无一例外。缺它不会报错——Python 3 会当成命名空间
  包——但会留下一个与本仓其余部分不一致的包）
- Create: `backend/app/jobs/cleanup_multi_step_runs.py`
- Test: `backend/app/tests/integration/test_multi_step_cleanup.py`

**Interfaces:**
- Produces: `async def cleanupMultiStepRuns(session, *, succeededRetentionDays: int = 30, failedRetentionDays: int = 7, now: datetime | None = None) -> int`

- [ ] **Step 1: 写失败测试**

`backend/app/tests/integration/test_multi_step_cleanup.py`:
```python
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.multi_step_models import MultiStepRun
from app.jobs.cleanup_multi_step_runs import cleanupMultiStepRuns
from app.tests import _pg_support


@pytest.fixture()
async def pgSession() -> AsyncIterator[AsyncSession]:
    """真实 PG 会话：每测试新建引擎 + TRUNCATE 隔离。

    本文件的断言是**全局**的（`deleted` 计数、剩余 run 集合），必须隔离。
    不能用 `db_session`：truncate 挂在 `pg_client` 上，`db_session` 不 truncate。
    """
    engine = await _pg_support._newEngine()
    try:
        await _pg_support._truncateAll(engine)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def testCleanupDeletesOnlyExpiredRuns(pgSession):
    from app.domain.research_models import ResearchSession

    sessionRow = ResearchSession(id=uuid.uuid4(), title="cleanup", created_by=1)
    pgSession.add(sessionRow)
    await pgSession.commit()

    now = datetime.now(UTC)

    async def addRun(status: str, ageDays: int) -> MultiStepRun:
        run = MultiStepRun(
            id=uuid.uuid4(), session_id=sessionRow.id, question="q", model_id=None,
            total_steps=1, status=status,
            started_at=now - timedelta(days=ageDays),
            updated_at=now - timedelta(days=ageDays),
            finished_at=now - timedelta(days=ageDays),
        )
        pgSession.add(run)
        await pgSession.flush()
        return run

    oldSucceeded = await addRun("succeeded", 40)   # 删
    freshSucceeded = await addRun("succeeded", 5)  # 留
    oldFailed = await addRun("failed", 10)         # 删
    freshFailed = await addRun("failed", 2)        # 留
    oldRunning = await addRun("running", 100)      # 留（未终态不删）
    await pgSession.commit()

    # Act
    deleted = await cleanupMultiStepRuns(pgSession, now=now)
    await pgSession.commit()

    # Assert
    assert deleted == 2
    remaining = {r.id for r in (await pgSession.execute(select(MultiStepRun))).scalars().all()}
    assert remaining == {freshSucceeded.id, freshFailed.id, oldRunning.id}


@pytest.mark.asyncio
async def testCleanupDeletesStepsViaCascade(pgSession):
    from app.domain.multi_step_models import MultiStepStep
    from app.domain.research_models import ResearchSession

    sessionRow = ResearchSession(id=uuid.uuid4(), title="cleanup-cascade", created_by=1)
    pgSession.add(sessionRow)
    await pgSession.commit()
    now = datetime.now(UTC)
    run = MultiStepRun(
        id=uuid.uuid4(), session_id=sessionRow.id, question="q", model_id=None,
        total_steps=1, status="succeeded",
        started_at=now - timedelta(days=60), updated_at=now - timedelta(days=60),
        finished_at=now - timedelta(days=60),
    )
    pgSession.add(run)
    await pgSession.flush()
    pgSession.add(MultiStepStep(id=uuid.uuid4(), run_id=run.id, step_index=0, status="succeeded", sub_question="a"))
    await pgSession.commit()

    await cleanupMultiStepRuns(pgSession, now=now)
    await pgSession.commit()

    assert (await pgSession.execute(select(MultiStepStep))).scalars().all() == []
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
git add backend/app/jobs/__init__.py backend/app/jobs/cleanup_multi_step_runs.py backend/app/tests/integration/test_multi_step_cleanup.py
git commit -m "feat(multi-step): 新增 run 保留期清理任务"
```

---

### Task 9: 前端续跑入口

**Files:**
- Modify: `frontend/src/types/chat.ts`（`StepStatus` 增 `compressed`；`MultiStepStep` 增 `runId`/`originalRows`/`compressedRows`）
- Modify: `frontend/src/api/chat.ts`（抽 `postSseStream`；`onStepPlanOverview` 带 runId；新增 `onStepCompressed`；新增 `resumeMultiStepRun`）
- Modify: `frontend/src/stores/chatStore.ts`（抽 `streamHandlers(set)`；新增 `resumeRun`；回填 runId 与压缩徽章）
- Create: `frontend/src/components/chat/ResumeRunButton.tsx`
- Modify: `frontend/src/components/chat/MultiStepPlanCard.tsx`（压缩徽章 + 失败步续跑按钮）
- Modify: `frontend/src/components/chat/MessageItem.tsx:133`（把 `resumeRun` 传进卡片）
- Modify: `frontend/src/i18n/zh-CN.ts`（`multiStep` 段）
- Modify: `frontend/src/i18n/en-US.ts`（`multiStep` 段）
- Test: `frontend/src/tests/MultiStepPlanCard.test.tsx`
- Test: `frontend/src/tests/chatApi.test.ts`

**Interfaces:**
- Consumes: Task 6 的 `multi_step_plan.runId` 与 `step_compressed` 事件；Task 7 的
  `POST /api/v1/chat/multi-step/{runId}/resume`（body camelCase `{fromStepIndex, modelOverride, compressAgain}`，
  header `Idempotency-Key`，响应是 SSE 流）
- Produces: 组件 `ResumeRunButton`（默认导出）；`MultiStepPlanCard` 新增可选 prop
  `onResume?: (runId: string, fromStepIndex: number) => void`；`api/chat.ts` 导出
  `resumeMultiStepRun(runId, fromStepIndex, handlers)`；store 新增动作 `resumeRun(runId, fromStepIndex)`

**范围（已与用户对齐）：** 只做 spec §8.2 的聊天面板部分 —— 失败步续跑按钮 + 压缩徽章。
§8.1 session 列表徽章、§8.3 续跑弹窗（含 `compressAgain` 复选框）、以及非流式渲染下的
runId（后端非流式响应不带）都不在本任务，已登记进 Task 10 的遗留项。

- [ ] **Step 1: 先读现状（不要靠猜字段名）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
sed -n '140,165p' frontend/src/types/chat.ts          # StepStatus / MultiStepStep
sed -n '224,245p' frontend/src/api/chat.ts            # StreamEventHandlers
sed -n '249,286p' frontend/src/api/chat.ts            # sendMessageStream（要被抽出的那段 fetch）
sed -n '368,378p' frontend/src/api/chat.ts            # multi_step_plan 分发
sed -n '103,131p' frontend/src/stores/chatStore.ts    # patchStep / patchLastMessage
sed -n '352,585p' frontend/src/stores/chatStore.ts    # sendMessage 里内联的 handlers
```

- [ ] **Step 2: 写失败测试（组件层）**

`frontend/src/tests/MultiStepPlanCard.test.tsx` —— 这文件已存在且形状良好，**沿用**它的
`renderExpanded` / `makeStep` / `PANEL_LABEL` / `vi.mock("echarts-for-react")`，不要另起炉灶。

把 `renderExpanded` 改成收一个可选回调（只加参数，既有调用不动）：
```tsx
function renderExpanded(
  steps: MultiStepStep[],
  onResume?: (runId: string, fromStepIndex: number) => void
) {
  render(<MultiStepPlanCard steps={steps} onResume={onResume} />);
  fireEvent.click(screen.getByText(PANEL_LABEL));
}
```
文件末尾追加：
```tsx
describe("MultiStepPlanCard 续跑与压缩徽章", () => {
  it("失败步骤带 runId 时渲染续跑按钮，点击回调带 runId 与步号", () => {
    // Arrange
    const onResume = vi.fn();
    renderExpanded(
      [makeStep({ stepIndex: 1, status: "error", error: "oMLX timeout", runId: "r-1" })],
      onResume
    );

    // Act
    fireEvent.click(screen.getByTestId("resume-run"));

    // Assert
    expect(onResume).toHaveBeenCalledWith("r-1", 1);
  });

  it("没有 runId 的失败步骤不渲染续跑按钮（单步路径不落库）", () => {
    const onResume = vi.fn();
    renderExpanded([makeStep({ status: "error", error: "boom" })], onResume);

    expect(screen.queryByTestId("resume-run")).toBeNull();
  });

  it("压缩步骤显示压缩徽章与行数", () => {
    renderExpanded([
      makeStep({ status: "compressed", originalRows: 1000, compressedRows: 30 }),
    ]);

    expect(screen.getByText("已压缩")).toBeInTheDocument();
    expect(screen.getByText("数据已压缩（1000 → 30 行）")).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd frontend && npm test -- --run src/tests/MultiStepPlanCard.test.tsx
```
Expected: FAIL —— `runId`/`originalRows`/`compressedRows` 不在 `MultiStepStep` 上（TS 报错），
且 `onResume` 不是 `MultiStepPlanCard` 的 prop。

- [ ] **Step 4: 写失败测试（API 层）**

`frontend/src/tests/chatApi.test.ts`（沿用文件里已有的 `sseStream` / `vi.stubGlobal("fetch", ...)` 手法）：
```tsx
it("multi_step_plan 携带 runId 时作为第二个参数交给回调", async () => {
  const stream = sseStream(
    'event: multi_step_plan\ndata: {"runId":"r-9","steps":[{"stepIndex":0,"description":"d","subQuestion":"q","aggregationOnly":false}]}\n\n'
  );
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

  const seen: Array<[unknown, unknown]> = [];
  await sendMessageStream(makePayload(), {
    onStepPlanOverview: (steps, runId) => seen.push([steps, runId]),
  });

  expect(seen).toHaveLength(1);
  expect(seen[0]?.[0]).toHaveLength(1);
  expect(seen[0]?.[1]).toBe("r-9");
});

it("multi_step_plan 不带 runId（单步路径）时第二个参数为 undefined", async () => {
  const stream = sseStream(
    'event: multi_step_plan\ndata: {"steps":[{"stepIndex":0,"description":"d","subQuestion":"q","aggregationOnly":false}]}\n\n'
  );
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

  const seen: unknown[] = [];
  await sendMessageStream(makePayload(), {
    onStepPlanOverview: (_steps, runId) => seen.push(runId),
  });

  expect(seen).toEqual([undefined]);
});

it("step_compressed 事件分发给 onStepCompressed", async () => {
  const stream = sseStream(
    'event: step_compressed\ndata: {"stepIndex":0,"originalRows":1000,"compressedRows":30}\n\n'
  );
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

  const seen: unknown[] = [];
  await sendMessageStream(makePayload(), {
    onStepCompressed: (payload) => seen.push(payload),
  });

  expect(seen).toEqual([{ stepIndex: 0, originalRows: 1000, compressedRows: 30 }]);
});

it("resumeMultiStepRun POST 到 resume 端点，带 Idempotency-Key 与 camelCase body", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: sseStream() }));

  await resumeMultiStepRun("r-1", 2, {});

  const [url, init] = vi.mocked(fetch).mock.calls[0];
  expect(String(url)).toContain("/chat/multi-step/r-1/resume");
  expect(init?.method).toBe("POST");
  expect(JSON.parse(String(init?.body))).toEqual({ fromStepIndex: 2 });
  // 幂等键由前端生成，后端据此去重（spec §7.3）
  expect(typeof (init?.headers as Record<string, string>)["Idempotency-Key"]).toBe("string");
});
```
（`resumeMultiStepRun` 与 `sendMessageStream` 一起从 `../api/chat` import。）

- [ ] **Step 5: 跑测试确认失败**

```bash
cd frontend && npm test -- --run src/tests/chatApi.test.ts
```
Expected: FAIL —— `resumeMultiStepRun` / `onStepCompressed` 未定义。

- [ ] **Step 6: 实现 —— 类型**

`frontend/src/types/chat.ts`：
```ts
export type StepStatus = "pending" | "running" | "done" | "error" | "compressed";
```
`MultiStepStep` 追加三个可选字段（都用 `?`：单步路径与非压缩步没有它们）：
```ts
  // 该步所属 run 的 id（multi_step_plan 事件盖章）；单步路径不落库 ⇒ undefined，
  // 也因此没有续跑按钮。
  runId?: string;
  // 被上下文压缩的步：徽章展示原始行数 → 保留行数（step_compressed 事件回填）
  originalRows?: number;
  compressedRows?: number;
```

- [ ] **Step 7: 实现 —— `api/chat.ts`**

`StepStatus` 扩了成员，`MultiStepPlanCard` 里两个 `Record<StepStatus, ...>` 会**编译报错** ——
这是故意的，它逼你把新状态的两处颜色/阶段补齐（Step 9）。

`StreamEventHandlers` 改两处 + 加一个：
```ts
  // 多步：完整计划概览 / 单个子步骤计划（进入执行）/ 单个子步骤结果
  // runId：Task 6 起 multi_step_plan 事件携带；单步路径不发 ⇒ undefined
  onStepPlanOverview?: (steps: StepPlanOverviewItem[], runId?: string) => void;
  onStepPlan?: (step: StepPlanView) => void;
  onStepResult?: (result: StepResultView) => void;
  // 多步：某个**更早**的步被上下文压缩（其 step_result 早已发过，故单独补一条）
  onStepCompressed?: (payload: StepCompressedView) => void;
```
新增视图类型（放在 `StepResultView` 旁边）：
```ts
// step_compressed 事件负载（Task 6 新增）：某个更早的步被压缩后补发
export interface StepCompressedView {
  stepIndex: number;
  originalRows: number;
  compressedRows: number;
}
```

把 `sendMessageStream` 里的 fetch 段抽成 `postSseStream`（**逐行搬**，不要重写解析逻辑）：
```ts
/** 通用 SSE POST：路径可变，解析/分发逻辑与 sendMessageStream 完全共用。 */
async function postSseStream(
  path: string,
  body: unknown,
  handlers: StreamEventHandlers,
  extraHeaders: Record<string, string> = {}
): Promise<void> {
  // 走裸 fetch（SSE 流式 axios 不友好）—— 不经 httpClient 拦截器，
  // 故用 authHeaders()（SSOT）手动注入 Authorization + X-Tenant-Id。
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json", ...extraHeaders }),
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(`请求失败 (HTTP ${response.status})`);
  }
  if (!response.body) {
    throw new Error(i18n.t("errors.noStreamSupport"));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = consumeFrames(buffer, handlers);
    }
    // 冲刷解码器缓冲的尾字节：最后一个分块可能截断多字节 UTF-8 字符，
    // 流结束后必须显式解码残留字节，否则该字符被静默丢弃（HIGH#1 修复）
    buffer += decoder.decode();
    consumeFrames(buffer, handlers);
  } finally {
    reader.releaseLock();
  }
}
```
`sendMessageStream` 收缩成一行转发（**签名与行为不变**，既有测试与调用方零改动）：
```ts
export async function sendMessageStream(
  payload: ChatRequest,
  handlers: StreamEventHandlers
): Promise<void> {
  return postSseStream(`${BASE}/stream`, payload, handlers);
}
```
新增续跑（`BASE` 与 `sendMessageStream` 同源；`encodeURIComponent` 防 runId 注入路径）：

**先核对后端 DTO 的字段命名**（Task 7 落地的，写错就是 422，不是静默失败）：
```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
grep -n "class ResumeRequest" -A 8 backend/app/api/v1/chat.py
grep -n "def to_camel\|class CamelModel" -A 6 backend/app/domain/schemas.py | head -30
```
实测结论（不必再猜）：`ResumeRequest(CamelModel)`，字段声明为 snake_case 的
`from_step_index`；`CamelModel` 的 `alias_generator=to_camel` 把对外别名生成成
`fromStepIndex`，同时 `populate_by_name=True` 让**别名与字段名两种写法都能收**。
所以前端发 `{ fromStepIndex }` 是对外别名的正解 —— 下面这段照抄，**不要**改成
`{ from_step_index }`（那样也能进，但就是与 Task 7 的注释里写死的那个名字不一致）。

```ts
/**
 * 续跑一个失败的多步 run（spec §7）。响应同样是 SSE 流，复用同一套帧解析。
 *
 * Idempotency-Key 由前端生成：后端据它去重，重复提交不会重跑（spec §7.3）。
 */
export async function resumeMultiStepRun(
  runId: string,
  fromStepIndex: number | undefined,
  handlers: StreamEventHandlers
): Promise<void> {
  return postSseStream(
    `${BASE}/multi-step/${encodeURIComponent(runId)}/resume`,
    { fromStepIndex },
    handlers,
    { "Idempotency-Key": crypto.randomUUID() }
  );
}
```
（`crypto.randomUUID()` 在本仓已被 `chatStore.ts:79` 用过，测试环境可用。）

分发器改两处：
```ts
    case "multi_step_plan":
      if (Array.isArray(d.steps)) {
        const steps = d.steps.filter(isStepPlanOverviewItem);
        if (steps.length) {
          // 单步路径不发 runId ⇒ undefined（前端据此不渲染续跑按钮）
          handlers.onStepPlanOverview?.(
            steps,
            typeof d.runId === "string" ? d.runId : undefined
          );
        }
      }
      break;
```
```ts
    case "step_compressed":
      if (isStepIndex(d.stepIndex)) {
        handlers.onStepCompressed?.({
          stepIndex: d.stepIndex,
          originalRows: typeof d.originalRows === "number" ? d.originalRows : 0,
          compressedRows: typeof d.compressedRows === "number" ? d.compressedRows : 0,
        });
      }
      break;
```

- [ ] **Step 8: 实现 —— `chatStore.ts`**

把 `sendMessage`（约 352–585 行）里内联的 handlers 字面量整体搬到模块级函数
`streamHandlers(set)`，`sendMessage` 改为 `await sendMessageStream(payload, streamHandlers(set))`。
**只搬不改**：每个 `set((state) => ...)` 原样保留（该区间内没有用到 `get`）。
这样续跑与首发共用同一套 handler，避免两处状态判据漂移（同 `stepStatusFromResult` 的注释所述）。

`resumeRun` 动作（加进 `ChatState` 接口与 store 实现）：
```ts
  resumeRun: (runId: string, fromStepIndex: number) => Promise<void>;
```
```ts
  resumeRun: async (runId, fromStepIndex) => {
    set({ loading: true, error: null });
    try {
      await resumeMultiStepRun(runId, fromStepIndex, streamHandlers(set));
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : i18n.t("errors.unknownError");
      set((state) => ({
        messages: patchLastMessage(state.messages, {
          content: msg,
          isError: true,
          isStreaming: false,
        }),
        loading: false,
        error: msg,
      }));
    } finally {
      set((state) => {
        const last = state.messages[state.messages.length - 1];
        if (!state.loading && !last?.isStreaming) return {};
        if (!last?.isStreaming) return { loading: false };
        return {
          messages: patchLastMessage(state.messages, { isStreaming: false }),
          loading: false,
        };
      });
    }
  },
```
（`error` 分支与 `finally` 兜底与 `sendMessage` 同形 —— 续跑断开也要复位 `loading`，
否则发送按钮永久禁用，同 HIGH#3。）

两个 handler 改为回填新字段：
```ts
          // 完整计划概览：建立各步骤（含汇总步骤），初始状态「待执行」
          // runId 盖到每个步骤上：续跑按钮要凭它拼 resume 端点
          onStepPlanOverview: (steps, runId) =>
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                steps: steps.map(
                  (s): MultiStepStep => ({
                    stepIndex: s.stepIndex,
                    description: s.description,
                    subQuestion: s.subQuestion,
                    aggregationOnly: s.aggregationOnly,
                    status: "pending",
                    runId,
                  })
                ),
              }),
            })),
```
```ts
          // 更早的步被压缩：补上徽章（该步的 step_result 早已把它置为 done/error，
          // 这里覆盖成 compressed —— 压缩发生在它成功之后，覆盖是正确方向）
          onStepCompressed: (payload) =>
            set((state) => ({
              messages: patchStep(state.messages, payload.stepIndex, {
                status: "compressed",
                originalRows: payload.originalRows,
                compressedRows: payload.compressedRows,
              }),
            })),
```

- [ ] **Step 9: 实现 —— 组件**

`frontend/src/components/chat/ResumeRunButton.tsx`（默认导出，与同目录组件一致）：
```tsx
import { Button } from "antd";
import { useTranslation } from "../../i18n";

interface ResumeRunButtonProps {
  runId: string;
  fromStepIndex: number;
  disabled?: boolean;
  onResume: (runId: string, fromStepIndex: number) => void;
}

/**
 * 失败步骤的续跑按钮（spec §8.2）。
 *
 * 只负责「点击时把 runId + 起始步号交出去」，不发请求 —— 请求由 store 的
 * resumeRun 统一发起，与首发共用同一套 SSE handler。
 */
export default function ResumeRunButton({
  runId,
  fromStepIndex,
  disabled,
  onResume,
}: ResumeRunButtonProps) {
  const { t } = useTranslation();
  return (
    <Button
      size="small"
      type="primary"
      disabled={disabled}
      onClick={() => onResume(runId, fromStepIndex)}
      data-testid="resume-run"
    >
      {t("multiStep.resume")}
    </Button>
  );
}
```

`MultiStepPlanCard.tsx` 四处改动：
1. props 加回调、两个 `Record<StepStatus, ...>` 加 `compressed`：
```tsx
const STATUS_TO_ANTD: Record<StepStatus, "wait" | "process" | "finish" | "error"> = {
  pending: "wait",
  running: "process",
  done: "finish",
  // 压缩发生在步骤成功之后 ⇒ 阶段上仍是「完成」，只是数据被裁过
  compressed: "finish",
  error: "error",
};

const STATUS_TAG_COLOR: Record<StepStatus, string> = {
  pending: "default",
  running: "processing",
  done: "success",
  compressed: "warning",
  error: "error",
};

interface MultiStepPlanCardProps {
  steps: MultiStepStep[];
  currentStepIndex?: number;
  /** 失败步的续跑回调；不传则不渲染续跑按钮（如历史回放、单步路径） */
  onResume?: (runId: string, fromStepIndex: number) => void;
}
```
2. `StatusBadge` 的 `labels` 加 `compressed: t("multiStep.statusCompressed")`。
3. description 里既有块的判断条件补上 `compressed`（否则压缩步的 SQL/图会整块消失），
   并在其**之前**插入压缩徽章行：
```tsx
                    {s.status === "compressed" && s.originalRows != null && s.compressedRows != null ? (
                      <Text type="warning" style={{ display: "block", marginTop: 4 }}>
                        {t("multiStep.compressedRows", {
                          from: s.originalRows,
                          to: s.compressedRows,
                        })}
                      </Text>
                    ) : null}
                    {s.status === "done" || s.status === "compressed" || s.status === "error" ? (
```
4. 失败步的按钮，放在 `s.error` 那行之后、`s.chartType` 之前：
```tsx
                        {s.status === "error" && s.runId && onResume ? (
                          <div style={{ marginTop: 6 }}>
                            <ResumeRunButton
                              runId={s.runId}
                              fromStepIndex={s.stepIndex}
                              onResume={onResume}
                            />
                          </div>
                        ) : null}
```
补 import：`import ResumeRunButton from "./ResumeRunButton";`

`MessageItem.tsx:133` 传入 store 的 `resumeRun`（在组件体里取一次）：
```tsx
const resumeRun = useChatStore((s) => s.resumeRun);
```
```tsx
                <MultiStepPlanCard
                  steps={message.steps}
                  currentStepIndex={message.currentStepIndex}
                  onResume={resumeRun}
                />
```
（`useChatStore` 的 import 该文件已有；只加一个 selector。）

- [ ] **Step 10: 实现 —— i18n**

`frontend/src/i18n/zh-CN.ts` 的 `multiStep`（约 376 行）加三个键：
```ts
    statusCompressed: "已压缩",
    resume: "续跑",
    compressedRows: "数据已压缩（{from} → {to} 行）",
```
`frontend/src/i18n/en-US.ts` 的 `multiStep`（约 371 行）对应加：
```ts
    statusCompressed: "Compressed",
    resume: "Resume",
    compressedRows: "Data compressed ({from} → {to} rows)",
```
占位符是**单花括号**（react-i18next 已配 `prefix: "{"`）—— 写成 `{{from}}` 会原样显示。

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

- [ ] **Step 11: 跑测试 + 类型检查 + 覆盖率门禁**

```bash
cd frontend
npm test -- src/tests/MultiStepPlanCard.test.tsx src/tests/chatApi.test.ts
npm test                       # 全量：确认没有既有用例被 streamHandlers 抽取改坏
npm run build                  # tsc -b：Record<StepStatus, ...> 漏了 compressed 会在这里炸
npm run lint
npm run test:coverage          # 全局门禁 lines/functions/branches/statements ≥ 80
```
Expected: 全绿；覆盖率不低于门禁（见 memory `qa-system-frontend-coverage-gate`）。
`npm test` 里若有与本次无关的既有红，先判断是否本任务引入，**不要**顺手改无关测试。

- [ ] **Step 12: Commit**

```bash
git add frontend/src/types/chat.ts frontend/src/api/chat.ts \
        frontend/src/stores/chatStore.ts \
        frontend/src/components/chat/ResumeRunButton.tsx \
        frontend/src/components/chat/MultiStepPlanCard.tsx \
        frontend/src/components/chat/MessageItem.tsx \
        frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts \
        frontend/src/tests/MultiStepPlanCard.test.tsx frontend/src/tests/chatApi.test.ts
git commit -m "feat(multi-step): 前端续跑按钮与步骤压缩徽章"
```
**逐个文件列路径，不要 `git add frontend/src`** —— 目录级 add 会把同一时间未完成的
其它改动一并提交进这个 commit。

---

### Task 10: 文档与变更记录

**Files:**
- Create: `Harness/wiki/chat_multi_step_persistence.md`
- Create: `Harness/changes/feat-multi-step-persist/summary.md`
- Modify: `Harness/index.md`（若存在索引则登记新条目）

- [ ] **Step 1: 写 wiki 条目**

`Harness/wiki/chat_multi_step_persistence.md`：按 `Harness/wiki/` 既有条目格式（frontmatter + 概述 + 详细说明 + 相关条目），内容涵盖：两张表的关系、状态机、压缩触发阈值 0.7、重试 3 次 1s/2s 退避、续跑端点与幂等、保留期 30/7 天、feature flag 名。链接 [[chat_multistep_flow]]、[[llm_retry_policy]]、[[research_session]]（按实际存在的条目名调整）。

- [ ] **Step 2: 写 change 记录**

`Harness/changes/feat-multi-step-persist/summary.md`：SSOT 记录，含 spec 链接、迁移号 0114、feature flag、新增文件清单、测试命令与结果、以及**全部**遗留项（照抄本节末尾「遗留项」清单，一条不漏 —— 特别是 Task 9 只做了 spec §8.2 的聊天面板部分，§8.1/§8.3 没做，别让 change 记录读起来像 UI 已完工）。

- [ ] **Step 3: Commit**

```bash
git add Harness/wiki/chat_multi_step_persistence.md Harness/changes/feat-multi-step-persist/summary.md Harness/index.md
git commit -m "docs(multi-step): 补 wiki 与 change 记录"
```

---

## 遗留项（本计划不做，但要在 change 记录里登记）

1. **续跑不做前序结果回灌（spec §7.2 step 5「plan 重放」未做）**：2026-10-05 裁决为
   「最小正确版」——续跑从 `current_step_idx` 起跑，跳过更早的步（但仍计入 `completed`），
   **不**把前序步的 `data` 拼回 prompt、**不**按 run 原 plan 重放规划。依据是 2026-09-28
   真机诊断已证伪「多步之间存在步间数据依赖」（见 `qa-system-multistep-no-data-dependency`）：
   各子问题独立查询、最后在报告层聚合，故回灌不产生正确性收益。真要做时须同批重审
   `adoptRunForResume` 的形状判定。
2. **`sql_hash` 命中缓存跳过 LLM（spec §7.2 step 6 未做）**：字段已落库，`adoptRunForResume`
   还**刻意**把 `sql` / `sql_hash` 清成 `None`，避免未来这个特性拿陈旧 hash 误命中。
   先保证正确性，再优化 token。
3. **压缩后仍 > 95% → 步转 `skipped`**：spec §4.1 定义了该状态，但触发点依赖
   `_planAndGenerateSql` 的实际报错形态；先按 permanent 处理，观察线上日志后再实现。
4. **`compress_again` 参数**：`ResumeRequest` 已接收但当前实现忽略（压缩只按阈值自动触发）。
5. **前端 `from_step_index` 选择弹窗**：Task 9 只做「默认从首个失败步续跑」；下拉选步延后。
6. **spec §8.1（session 列表「未完成」徽章）未做**：需要 session 列表接口回传 run 状态，
   现接口不返回，改动面超出本计划。
7. **spec §8.3（续跑弹窗：`from_step_index` 下拉 + `compressAgain` 复选框）未做**：
   Task 9 直接把 `fromStepIndex` 定为失败步号、`compressAgain` 固定 false。API 两端
   都已支持这两个参数（Task 7 的 `ResumeRequest`），只是前端没有入口。
8. **非流式渲染下的续跑入口缺失**：`runId` 与「已压缩」信息都只走 SSE（Task 6 增量）。
   非流式 `/chat` 的 `steps` 负载不带这两项，故非流式回答里既没有续跑按钮也没有压缩徽章。
   补齐需要改 `_executeMultiStep` 的读模型构造 + `ChatResponse.steps` 的元素类型。
9. **压缩徽章的「展开原始数据」未做**：spec §8.2 要求 `[展开原始数据]` 链到
   `multi_step_step.data`。需要新增 `GET /chat/multi-step/{runId}/steps/{stepIndex}/data`
   （含归属校验 + 分页），本计划没有这个端点，故徽章目前只是提示。
10. **SSE 中断后前端自动重连续跑**：依赖前端的 SSE 封装改造，单独排期。后端一侧已就绪：
    Task 7 的 `_sealAbandonedResume` 在 `finally` 里封口，断连不会留下 `running` 僵尸。
11. **超大 data（> 5MB）转对象存储**：spec §14 提到超限走 minio，但当前 `data` 一律进 JSONB。
    先观察真实 `pg_column_size(multi_step_step.data)` 分布，确认有超限样本后再实现，
    避免过早引入存储依赖。
12. **并发续跑乐观锁的落库侧强约束**：当前靠 `run.version++` 的自增语义 + 状态校验挡住
    大部分并发，但**没有** `SELECT … FOR UPDATE`，极端并发下两个请求都可能通过校验。
    若线上出现双跑，再补行级锁。
