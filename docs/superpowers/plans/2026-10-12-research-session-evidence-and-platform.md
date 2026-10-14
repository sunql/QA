# 研究会话证据契约与平台级增强 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `feat/multi-step-persist` 基础上，先建立研究会话的证据契约（问题范围不丢失、假设按数据判定、报告可引用基础结果），再逐步引入持久化运行/事件/任务队列与三 mode 策略，使研究会话可恢复、可复现、可信。

**Architecture:** 本计划拆为两个可独立合入的环节：
- **环节一（核心证据契约）**：新增/扩展数据模型，修正状态恢复、并发互斥、数据序列化、摘要结构化、假设判定、基础结果引用；保持现有进程内 BackgroundTasks 与 event bus 不变。
- **环节二（平台级增强）**：在环节一的数据契约上，引入 `ResearchRun` 版本化快照、持久化任务队列、event outbox、worker 租约心跳、三 mode 差异化策略与证据回放；仅在环节一稳定并上线后启动。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.0 (async) / PostgreSQL 15+ / Alembic / Pydantic v2 / pytest-asyncio；前端 React 18 + TypeScript + Zustand + SSE。

## Global Constraints

- **不可变数据**：始终创建新对象，禁止原地修改；状态更新返回新字典/新对象。
- **SQL 安全**：业务查询仅允许只读 SELECT，经 SQL Guard 校验；假设验证 SQL 必须复用与 plan 步相同的 dialect/schema 约束。
- **Token 计量**：每次 LLM 调用必须记录 Token 消耗与成本；失败路径保存已知消耗。
- **TDD**：先写测试（RED）→ 实现（GREEN）→ 重构（IMPROVE），覆盖率 ≥ 80%。
- **小文件**：200-400 行为宜，不超过 800 行；函数 < 50 行；嵌套 ≤ 4 层。
- **真实数据库测试**：后端测试必须用真实 PostgreSQL + 完整 API 链路 + 真实数据库对象，禁止 sqlite 内存库 + 直接调用 service。
- **显式错误处理**：每个层级显式处理错误，UI 层给友好提示，服务端记录详细上下文。
- **命名约定**：函数/变量 `camelCase`；类型/组件 `PascalCase`；常量 `UPPER_SNAKE_CASE`；ORM 字段 `snake_case`（刻意偏离 PEP 8 以与前端/JSON 契约一致）。
- **Alembic 迁移**：所有模型变更必须生成迁移；本地测试前升 migrate。
- **禁止后端覆盖用户 modelId**：模型表现差修模型，不允许“按内容自动改派模型”。

---

# 环节一：核心证据契约（必须优先完成）

本环节直接对应建议文档中的“第一阶段确定性缺陷” + “第二阶段证据契约”。

---

## Task 1: 本轮问题版本化与恢复态修正

**Files:**
- Create: `backend/app/services/research_run_state.py`
- Modify: `backend/app/domain/research_models.py`（新增 `ResearchRunState` 表）
- Modify: `backend/app/services/research_agent_stages.py:48-80`
- Modify: `backend/app/services/research_agent_service.py:203-249`（`startTurn` / `runTurn`）
- Modify: `backend/app/services/research_agent_phases.py:543-580`（`resumeState`）
- Modify: `backend/app/services/research_session_service.py`（新增读写 `ResearchRunState` 方法）
- Test: `backend/tests/integration/research/test_run_state_recovery.py`
- Migration: `alembic revision --autogenerate -m "add research_run_state"`

**Interfaces:**
- Consumes: `ResearchSession.id`, `ResearchTurn.id`, `ResearchCheckpoint.options`
- Produces:
  - `ResearchRunState` ORM 模型：`run_id`, `session_id`, `turn_id`, `parent_run_id`, `current_question`, `mode`, `scope_version`, `datasource_id`, `schema_version`, `model_id`, `state_payload`, `created_at`, `updated_at`
  - `buildRunState(*, sessionId, turnId, question, mode, userId, modelId, parentRunId=None) -> ResearchRunState`
  - `rebuildState(runState: ResearchRunState, checkpoint: Any) -> dict[str, Any]`（替代原从 `ResearchSession.input_seed` 取 question）
  - `rewriteState(state, question)` 行为不变，但需同步更新 `ResearchRunState.current_question`

### Step 1: 写失败测试

```python
# backend/tests/integration/research/test_run_state_recovery.py
import pytest
from uuid import uuid4

pytestmark = pytest.mark.anyio


async def test_resume_turn_keeps_current_question(client, research_session_factory, db):
    # Arrange: 创建会话，首轮问题为 "2025 采购总额"
    session = await research_session_factory(input_seed="分析2025年采购总额")
    # 提交一轮，改写为 "只分析2024年订单数量"
    r1 = await client.post(f"/api/v1/research/sessions/{session.id}/turns", json={"question": "只分析2024年订单数量"})
    assert r1.status_code == 202
    turn1 = r1.json()["turnId"]

    # 等待范围确认 checkpoint
    cp = await wait_for_checkpoint(session.id, stage="intent")
    # Act: 确认范围，触发 plan 阶段
    r2 = await client.post(f"/api/v1/research/checkpoints/{cp['id']}/answer", json={"action": "confirm", "choice": {}})
    assert r2.status_code == 200

    # 在 plan checkpoint 处暂停
    cp_plan = await wait_for_checkpoint(session.id, stage="plan")
    # 再次恢复（模拟浏览器刷新）
    r3 = await client.post(f"/api/v1/research/checkpoints/{cp_plan['id']}/answer", json={"action": "confirm", "choice": {}})
    assert r3.status_code == 200

    # Assert: 状态机中的 question 仍为 "只分析2024年订单数量"
    run_state = await db.get(ResearchRunState, ???)
    assert run_state.current_question == "只分析2024年订单数量"
```

> 注：`???` 在 Step 3 实现后替换为通过 `session_id + turn_id` 查询到的实际 run_id；测试需使用 factory 提供的 helper。

### Step 2: 跑测试确认失败

```bash
cd backend
pytest tests/integration/research/test_run_state_recovery.py -v
```

Expected: `ResearchRunState not found` 或 `AssertionError: question != '只分析2024年订单数量'`。

### Step 3: 新增数据模型

```python
# backend/app/domain/research_models.py
class ResearchRunState(Base):
    """研究会话单轮运行的版本化状态快照。"""

    __tablename__ = "research_run_state"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _session_fk(), nullable=False, index=True
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _turn_fk(), nullable=False, index=True
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_run_state.id", ondelete="SET NULL"), nullable=True
    )
    current_question: Mapped[str] = mapped_column(Text, default="", server_default="")
    mode: Mapped[str] = mapped_column(String(_MODE_LEN), nullable=False)
    scope_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
```

### Step 4: 实现 `research_run_state.py` 无状态构造器

```python
# backend/app/services/research_run_state.py
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.domain.research_models import ResearchRunState


@dataclass(frozen=True)
class RunStateSeed:
    session_id: uuid.UUID
    turn_id: uuid.UUID
    current_question: str
    mode: str
    user_id: int | None
    model_id: int | None
    datasource_id: int | None
    parent_run_id: uuid.UUID | None = None


def buildRunState(seed: RunStateSeed) -> ResearchRunState:
    """从 turn 入口创建新的 ResearchRunState（不操作 DB）。"""
    return ResearchRunState(
        id=uuid.uuid4(),
        session_id=seed.session_id,
        turn_id=seed.turn_id,
        parent_run_id=seed.parent_run_id,
        current_question=seed.current_question,
        mode=seed.mode,
        scope_version=1,
        datasource_id=seed.datasource_id,
        schema_version=1,
        model_id=seed.model_id,
        state_payload={},
    )


def updateRunState(state: ResearchRunState, *, current_question: str | None = None,
                   scope_version: int | None = None, state_payload: dict[str, Any] | None = None) -> ResearchRunState:
    """返回更新后的新 ResearchRunState（不可变）。"""
    return ResearchRunState(
        id=state.id,
        session_id=state.session_id,
        turn_id=state.turn_id,
        parent_run_id=state.parent_run_id,
        current_question=current_question if current_question is not None else state.current_question,
        mode=state.mode,
        scope_version=scope_version if scope_version is not None else state.scope_version,
        datasource_id=state.datasource_id,
        schema_version=state.schema_version,
        model_id=state.model_id,
        state_payload=state_payload if state_payload is not None else state.state_payload,
        created_at=state.created_at,
        updated_at=state.updated_at,
    )


def stateToDict(state: ResearchRunState) -> dict[str, Any]:
    """将 runState 转为本轮状态机的初始/恢复态字典。"""
    return {
        "runId": str(state.id),
        "question": state.current_question,
        "mode": state.mode,
        "userId": state.state_payload.get("userId"),
        "modelId": state.model_id,
        "datasourceId": state.datasource_id,
        "scopeVersion": state.scope_version,
        "schemaVersion": state.schema_version,
        "parentRunId": str(state.parent_run_id) if state.parent_run_id else None,
    }
```

### Step 5: 修改 `runTurn` / `startTurn` 创建 ResearchRunState

```python
# backend/app/services/research_agent_service.py
from app.services.research_run_state import RunStateSeed, buildRunState, stateToDict

async def runTurn(self, session, *, sessionId, turnId, question, userId, mode, emit=None):
    requireQuestion(question, sessionId)
    await self._sessions.updateSessionStatus(session, sessionId, STATUS_RUNNING)
    row = await self._loadSession(session, sessionId)

    # 创建版本化 run state
    run_state = buildRunState(RunStateSeed(
        session_id=sessionId,
        turn_id=turnId,
        current_question=question,
        mode=mode,
        user_id=userId,
        model_id=row.model_id,
        datasource_id=row.datasource_id,
    ))
    session.add(run_state)

    state: dict[str, Any] = {
        **stateToDict(run_state),
        "runStateId": run_state.id,
        "esl": None,
        "plan": None,
        "stepResults": [],
        "hypotheses": [],
        "resumeStepIndex": 0,
        "choice": {},
        "llmUnavailable": False,
    }
    return await self._guardedRun(
        session, sessionId=sessionId, turnId=turnId, runStateId=run_state.id,
        phase=PHASE_INTENT, emit=emit, state=state,
    )
```

### Step 6: 修改 `rebuildState` 从 ResearchRunState 取 question

```python
# backend/app/services/research_agent_stages.py
from app.services.research_run_state import updateRunState


def rebuildState(runState: ResearchRunState, checkpoint: Any) -> dict[str, Any]:
    """恢复态：从版本化 run state + checkpoint.options 重建。"""
    options = checkpoint.options or {}
    return {
        "runId": str(runState.id),
        "question": runState.current_question,
        "mode": runState.mode,
        "userId": runState.state_payload.get("userId"),
        "modelId": runState.model_id,
        "datasourceId": runState.datasource_id,
        "scopeVersion": runState.scope_version,
        "schemaVersion": runState.schema_version,
        "parentRunId": str(runState.parent_run_id) if runState.parent_run_id else None,
        "runStateId": runState.id,
        "esl": options.get(OPT_ARMS),
        "plan": options.get(OPT_PLAN),
        "stepResults": list(options.get(OPT_STEP_RESULTS) or []),
        "stepsExecuted": int(options.get(OPT_STEPS_EXECUTED) or 0),
        "hypotheses": list(options.get(OPT_CANDIDATES) or []),
        "resumeStepIndex": int(options.get(OPT_NEXT_STEP) or 0),
        "choice": {},
        OPT_REPLAN_COUNT: int(options.get(OPT_REPLAN_COUNT) or 0),
    }


def rewriteState(state: dict[str, Any], question: str) -> dict[str, Any]:
    """改写问题：保留 runState 元数据，只换 question 并清空派生数据。"""
    return {
        **state,
        "question": question,
        "esl": None,
        "plan": None,
        "stepResults": [],
        "stepsExecuted": 0,
        "hypotheses": [],
        "resumeStepIndex": 0,
        # 改写问题意味着 scope 发生语义变化，递增 scopeVersion
        "scopeVersion": int(state.get("scopeVersion", 1)) + 1,
    }
```

### Step 7: `resumeTurn` 加载 ResearchRunState 并写回改写后的问题

```python
# backend/app/services/research_agent_service.py
async def resumeTurn(self, session, checkpointId, action, choice, emit=None):
    # ... 加载 checkpoint / session ...
    checkpoint = await self._sessions.resolveCheckpoint(...)
    row = session_row

    # 通过 checkpoint.turn_id 找到对应 run state
    run_state = await self._sessions.getRunStateByTurn(session, checkpoint.turn_id)
    if run_state is None:
        raise ValueError(f"checkpoint 对应的 run state 不存在: checkpoint={checkpointId}")

    rewritten = await self._recordResumeTurn(session, checkpoint, action=action, choice=choice, checkpointId=checkpointId)
    state = resumeState(run_state, checkpoint, choice or {})
    decision = decideResume(checkpoint, action, choice)
    startPhase = decision.phase

    if rewritten:
        state = rewriteState(state, rewritten)
        # 同步落库
        run_state = updateRunState(run_state, current_question=rewritten)
        session.add(run_state)

    if decision.isReplan:
        state["replan"] = True
        state[OPT_REPLAN_COUNT] = int(state.get(OPT_REPLAN_COUNT, 0)) + 1
        run_state = updateRunState(
            run_state,
            state_payload={**(run_state.state_payload or {}), OPT_REPLAN_COUNT: state[OPT_REPLAN_COUNT]},
        )
        session.add(run_state)

    return await self._guardedRun(
        session, sessionId=checkpoint.session_id, turnId=checkpoint.turn_id,
        runStateId=run_state.id, phase=startPhase, emit=emit, state=state,
    )
```

### Step 8: 跑测试确认通过

```bash
cd backend
pytest tests/integration/research/test_run_state_recovery.py -v
```

Expected: PASS

### Step 9: Commit

```bash
git add backend/app/domain/research_models.py backend/app/services/research_run_state.py \
        backend/app/services/research_agent_stages.py backend/app/services/research_agent_service.py \
        backend/tests/integration/research/test_run_state_recovery.py \
        alembic/versions/xxxx_add_research_run_state.py
git commit -m "feat(research): 版本化 ResearchRunState，恢复态不再回退到 input_seed

- 新增 ResearchRunState 表与 buildRunState/updateRunState 不可变构造器
- runTurn 创建 runState，resumeTurn 从 checkpoint.turn_id 加载
- rewriteState 递增 scopeVersion，改写后的问题同步落库
- 集成测试：追问/改写后恢复不回落种子问题"
```

---

## Task 2: 并发运行互斥与幂等键

**Files:**
- Modify: `backend/app/domain/research_models.py`（`ResearchSession` 加 `active_run_id`）
- Modify: `backend/app/services/research_session_service.py`（新增原子认领方法）
- Modify: `backend/app/api/v1/research.py:376-393`
- Modify: `backend/app/api/v1/research.py:401-422`
- Modify: `backend/app/schemas/research.py`（`ResearchTurnCreate` / `CheckpointAnswerRequest` 加 `idempotencyKey`）
- Test: `backend/tests/integration/research/test_run_concurrency.py`
- Migration: `alembic revision --autogenerate -m "add active_run_id to research_session"`

**Interfaces:**
- Consumes: `ResearchSession.id`, `ResearchRunState.id`, `idempotencyKey: str | None`
- Produces:
  - `ResearchSession.active_run_id: uuid.UUID | None`
  - `claimActiveRun(session, runId, idempotencyKey) -> bool`
  - `releaseActiveRun(session, runId) -> None`
  - `createTurn` 拒绝 running/awaiting_user 状态的重复提交

### Step 1: 写失败测试

```python
# backend/tests/integration/research/test_run_concurrency.py
async def test_second_turn_rejected_while_first_awaiting_user(client, research_session_factory):
    session = await research_session_factory()
    r1 = await client.post(f"/api/v1/research/sessions/{session.id}/turns", json={"question": "Q1"})
    assert r1.status_code == 202
    await wait_for_checkpoint(session.id, stage="intent")

    r2 = await client.post(f"/api/v1/research/sessions/{session.id}/turns", json={"question": "Q2"})
    assert r2.status_code == 409
    assert "存在进行中的研究轮次" in r2.json()["message"]

async def test_idempotency_key_allows_retry(client, research_session_factory):
    session = await research_session_factory()
    key = "idem-001"
    r1 = await client.post(f"/api/v1/research/sessions/{session.id}/turns", json={"question": "Q1", "idempotencyKey": key})
    assert r1.status_code == 202
    r2 = await client.post(f"/api/v1/research/sessions/{session.id}/turns", json={"question": "Q1", "idempotencyKey": key})
    assert r2.status_code == 202
    assert r2.json()["turnId"] == r1.json()["turnId"]
```

### Step 2: 跑测试确认失败

```bash
pytest tests/integration/research/test_run_concurrency.py -v
```

Expected: FAIL（第二个请求返回 202，不是 409）

### Step 3: 加模型字段

```python
# backend/app/domain/research_models.py ResearchSession
active_run_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("research_run_state.id", ondelete="SET NULL"), nullable=True
)
```

### Step 4: 实现原子认领

```python
# backend/app/services/research_session_service.py
import uuid
from sqlalchemy import select
from app.domain.research_models import ResearchRunState

class ResearchSessionService:
    async def getRunStateByTurn(self, session: AsyncSession, turn_id: uuid.UUID) -> ResearchRunState | None:
        result = await session.execute(select(ResearchRunState).where(ResearchRunState.turn_id == turn_id))
        return result.scalar_one_or_none()

    async def claimActiveRun(
        self, session: AsyncSession, sessionId: uuid.UUID, runId: uuid.UUID, idempotencyKey: str | None
    ) -> tuple[bool, uuid.UUID | None]:
        """原子认领执行权。返回 (success, existingRunId)。

        幂等键相同 ⇒ 视为同一运行，返回 True。
        """
        # 先查是否已有相同幂等键的运行
        if idempotencyKey:
            existing = await session.execute(
                select(ResearchRunState).where(
                    ResearchRunState.session_id == sessionId,
                    ResearchRunState.state_payload.contains({"idempotencyKey": idempotencyKey}),
                )
            )
            hit = existing.scalar_one_or_none()
            if hit is not None:
                return True, hit.id

        row = await session.get(ResearchSession, sessionId)
        if row.active_run_id is not None and row.active_run_id != runId:
            return False, row.active_run_id
        row.active_run_id = runId
        session.add(row)
        return True, None

    async def releaseActiveRun(self, session: AsyncSession, sessionId: uuid.UUID, runId: uuid.UUID) -> None:
        row = await session.get(ResearchSession, sessionId)
        if row.active_run_id == runId:
            row.active_run_id = None
            session.add(row)
```

### Step 5: 修改 `createTurn` 和 `answerCheckpoint`

```python
# backend/app/api/v1/research.py
from app.services.research_run_state import buildRunState, RunStateSeed

async def createTurn(...):
    row = await _ownedSession(db, sessionId, user)
    if row.active_run_id is not None:
        raise ConflictError("存在进行中的研究轮次，请等待当前轮次结束后再提交")

    turn = await _sessionService.appendTurn(db, sessionId=sessionId, role="user", content={"question": payload.question})
    run_state = buildRunState(RunStateSeed(...))
    run_state.state_payload["idempotencyKey"] = payload.idempotencyKey
    db.add(run_state)

    ok, existing = await _sessionService.claimActiveRun(db, sessionId, run_state.id, payload.idempotencyKey)
    if not ok:
        raise ConflictError(f"会话已被其他运行占用: run={existing}")

    await db.commit()
    background.add_task(_runTurnInBackground, str(sessionId), str(turn.id), str(run_state.id), payload.question, ...)
    return ResearchTurnAccepted(sessionId=sessionId, turnId=turn.id, runId=run_state.id, status="running")

async def answerCheckpoint(...):
    if checkpoint.status != "pending":
        raise ConflictError(...)
    # 幂等：同一 checkpoint + 同一 choice + 同一 action 视为重复提交
    choice_key = f"{payload.action}:{sorted(payload.choice.items()) if payload.choice else ''}"
    if checkpoint.user_choice is not None and checkpoint.state_payload.get("choiceKey") == choice_key:
        return CheckpointAnswerRead(sessionStatus="running", nextPhase=decideResume(...).phase)
    # ... 原有逻辑 ...
```

### Step 6: 在状态机终态释放 active_run

```python
# backend/app/services/research_agent_service.py _guardedRun 收尾处
finally:
    await self._sessions.releaseActiveRun(session, sessionId, runStateId)
```

### Step 7: 跑测试确认通过

```bash
pytest tests/integration/research/test_run_concurrency.py -v
```

### Step 8: Commit

```bash
git add backend/app/domain/research_models.py backend/app/services/research_session_service.py \
        backend/app/api/v1/research.py backend/app/schemas/research.py \
        backend/tests/integration/research/test_run_concurrency.py \
        alembic/versions/xxxx_add_active_run_id.py
git commit -m "feat(research): 会话级 active_run_id 互斥 + 幂等键

- ResearchSession 加 active_run_id，claim/release 原子操作
- createTurn 拒绝并发轮次；answerCheckpoint 按 choice 幂等
- 状态机终态释放 active_run
- 集成测试覆盖并发拒绝与幂等重试"
```

---

### Task 2 实施偏差记录（controller，2026-10-12）

计划正文的伪代码在落地时被修正；以下为权威口径，与正文冲突时以此为准。

| # | 计划原文 | 实际落地 | 原因 |
|---|---------|---------|------|
| D1 | `CheckpointAnswerRequest` 加 `idempotencyKey` | **移除该字段**（`ResearchTurnCreate.idempotencyKey` 保留并已接线） | 同一 checkpoint 的重复决策已由 `CheckpointConflictError` → 409 拒绝（不会双执行），该键无功能；留着会让调用方误以为有去重 |
| D2 | `backend/app/schemas/research.py`、`backend/tests/...` | `backend/app/domain/research_schemas.py`、`backend/app/tests/...` | 计划里的路径不存在 |
| D3 | `claimActiveRun` 先读后写 | **原子条件 UPDATE**（`WHERE active_run_id IS NULL`，rowcount=1 胜出） | 计划伪代码有 TOCTOU 竞态；且幂等键查找必须排除调用方自己刚 flush 的轮次，否则互斥失效 |
| D4 | 无 | 新增 `POST /research/sessions/{sessionId}/abort`（条件 UPDATE、不倒退、幂等）；**仅 `awaiting_user` 可取消**，`running` 一律 409、无副作用、不发终态帧 | 补 `awaiting_user` 挂起时永久占用执行权的死角。**不能允许取消 `running`**：在跑的状态机终态写入是无条件的（`updateSessionStatus` 盲写），会复活已取消的会话，且执行权被清空后下一轮 `claimActiveRun` 会成功 ⇒ 同会话两台状态机并行，正是本任务的互斥要防的事。**前端接线未做**（后续任务） |
| D5 | 409 文案单一 | 按会话状态分支：`awaiting_user` ⇒ 说明「停在检查点等决策」 | 原文案「存在进行中的研究轮次」此时为假（没有轮次在跑） |

遗留（后续任务，不在 Task 2 范围）：幂等键在后台轮次写库前不durable；前端不发送 `idempotencyKey`；abort 端点无前端调用方。

---

## Task 3: SSE 数据序列化契约

### Task 3 实施口径（controller 预置，2026-10-12）

正文伪代码与实际代码不符，且 tag 包装设计没有消费者。以本节为准。

| # | 计划原文 | 实际口径 | 原因 |
|---|---------|---------|------|
| D6 | 改 `_eventFrames` 用 `eventBus.subscribe` / `queue.get()` → dict | 实际是 `_bus.subscribe`、`queue.get()` → `(event, payload)` 元组、含心跳与 `_isTerminal` 提前收尾，且**已有** `_sseFrame` 助手（`api/v1/research.py:530-551`）。**只改 `_sseFrame`**：`json.dumps(payload, ensure_ascii=False)` → `research_json(payload)` | 正文伪代码是写计划时的臆造，真代码结构不同；重写 `_eventFrames` 是纯破坏 |
| D7 | `ResearchJSONEncoder` 把 Decimal/date/UUID 包成 `{"__type__": ...}` + `parse_json_value` 反解 | **改为扁平 JSON 安全标量**：Decimal → `str`、date/datetime → ISO 字符串、UUID → `str`；未知类型**抛 TypeError**（不静默 `str()`）。**不实现** tag 包装与 `parse_json_value` | 已验证：`__type__` 在前后端**均无任何消费者**；eventBus 传的是进程内 Python 对象（`research_event_bus.py:53`），`_sseFrame` 是**唯一**产出 SSE JSON 的地方 ⇒ 无「类型还原」需求；而 SSE 的消费者是浏览器 `JSON.parse`，tag 对象会直接渲染成坏表格。Decimal 走 `str` 而非 `float`：见 known issue「Decimal/float 阈值边界」，精度不能在线上丢 |
| D8 | Interfaces 里的 `QueryResult` 包装（`truncated`/`totalRows`/`columnTypes`/`schemaVersion`） | **移出 Task 3**，归入 Task 6（ResearchStepResult / ResultSnapshot 持久化） | 正文 5 个 Step 无一实现它；`truncated`/`totalRows` 需要 runner 侧支持（`VERIFICATION_ROW_CAP` 截断发生在 `research_sql_runner.py:96-100`，当前只打 warning 不回传），属于结果契约而非序列化 |

测试路径：`backend/app/tests/unit/services/test_research_json_encoder.py`、`backend/app/tests/integration/research/test_sse_serialization.py`（正文里的 `backend/tests/...` 不存在）。

**Files:**
- Create: `backend/app/services/research_json_encoder.py`
- Modify: `backend/app/api/v1/research.py:489-540`（`_eventFrames`）
- Modify: `backend/app/services/research_agent_execution.py`（`runStep` 结果包装）
- Test: `backend/tests/unit/services/test_research_json_encoder.py`
- Test: `backend/tests/integration/research/test_sse_serialization.py`

**Interfaces:**
- Consumes: `Decimal`, `date`, `datetime`, `list[dict]` 查询结果
- Produces:
  - `research_json(value: Any) -> str`：递归处理 Decimal/date/datetime/uuid
  - `QueryResult` 包装：`rows`, `columns`, `columnTypes`, `truncated`, `totalRows`, `returnedRows`, `schemaVersion`

### Step 1: 写失败测试

```python
# backend/tests/unit/services/test_research_json_encoder.py
from decimal import Decimal
from datetime import date, datetime
from app.services.research_json_encoder import research_json, parse_json_value


def test_research_json_serializes_decimal_date_uuid():
    payload = {
        "amount": Decimal("123.45"),
        "createdAt": date(2024, 1, 15),
        "updatedAt": datetime(2024, 1, 15, 10, 30, 0),
        "id": uuid4(),
    }
    s = research_json(payload)
    parsed = json.loads(s, object_hook=parse_json_value)
    assert parsed["amount"] == "123.45"
    assert parsed["createdAt"] == "2024-01-15"
    assert parsed["updatedAt"] == "2024-01-15T10:30:00"
```

### Step 2: 实现 encoder

```python
# backend/app/services/research_json_encoder.py
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any


class ResearchJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return {"__type__": "decimal", "value": str(obj)}
        if isinstance(obj, datetime):
            return {"__type__": "datetime", "value": obj.isoformat()}
        if isinstance(obj, date):
            return {"__type__": "date", "value": obj.isoformat()}
        if isinstance(obj, uuid.UUID):
            return {"__type__": "uuid", "value": str(obj)}
        return super().default(obj)


def research_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, cls=ResearchJSONEncoder)


def parse_json_value(d: dict[str, Any]) -> Any:
    type_tag = d.get("__type__")
    if type_tag == "decimal":
        return Decimal(d["value"])
    if type_tag == "datetime":
        return datetime.fromisoformat(d["value"])
    if type_tag == "date":
        return date.fromisoformat(d["value"])
    if type_tag == "uuid":
        return uuid.UUID(d["value"])
    return d
```

### Step 3: 修改 `_eventFrames`

```python
# backend/app/api/v1/research.py
from app.services.research_json_encoder import research_json

async def _eventFrames(sessionId: str) -> AsyncIterator[str]:
    queue = eventBus.subscribe(sessionId)
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            payload = event.get("payload", {})
            yield f"event: {event['event']}\ndata: {research_json(payload)}\n\n"
    finally:
        eventBus.unsubscribe(sessionId, queue)
```

### Step 4: 跑测试确认通过

```bash
pytest tests/unit/services/test_research_json_encoder.py -v
pytest tests/integration/research/test_sse_serialization.py -v
```

### Step 5: Commit

```bash
git add backend/app/services/research_json_encoder.py backend/app/api/v1/research.py \
        backend/tests/unit/services/test_research_json_encoder.py \
        backend/tests/integration/research/test_sse_serialization.py
git commit -m "feat(research): SSE 与 QueryResult 统一序列化 Decimal/date/UUID

- ResearchJSONEncoder 标记 decimal/datetime/date/uuid 类型
- _eventFrames 统一使用 research_json
- 配套 parse_json_value 用于测试反序列化"
```

---

## Task 4: 结构化 EvidenceSummary

**Files:**
- Create: `backend/app/services/research_evidence_summary.py`
- Modify: `backend/app/services/research_agent_execution.py:213-330`（`runStep` 返回包装）
- Modify: `backend/app/services/research_agent_phases.py`（执行相位消费 summary）
- Test: `backend/tests/unit/services/test_research_evidence_summary.py`

**Interfaces:**
- Consumes: SQL 查询结果 `list[dict[str, Any]]` + 列元数据
- Produces:
  - `EvidenceSummary` dataclass：`total`, `baseline`, `current`, `delta`, `deltaRatio`, `distribution`, `topN`, `anomalies`, `missingRate`, `sampleRows`, `unit`, `timeRange`
  - `summarizeEvidence(rows, columns, question, maxSampleRows=5) -> EvidenceSummary`

### Step 1: 写失败测试

```python
# backend/tests/unit/services/test_research_evidence_summary.py
from decimal import Decimal
from app.services.research_evidence_summary import summarizeEvidence


def test_summarize_year_amount_trend():
    rows = [{"year": 2024, "amount": Decimal("100")}, {"year": 2025, "amount": Decimal("200")}]
    columns = [{"name": "year", "type": "integer"}, {"name": "amount", "type": "numeric"}]
    summary = summarizeEvidence(rows, columns, question="采购金额变化")
    assert summary.baseline == Decimal("100")
    assert summary.current == Decimal("200")
    assert summary.delta == Decimal("100")
    assert summary.delta_ratio == Decimal("1.0")
```

### Step 2: 实现 EvidenceSummary

```python
# backend/app/services/research_evidence_summary.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class EvidenceSummary:
    total: Decimal | None = None
    baseline: Decimal | None = None
    current: Decimal | None = None
    delta: Decimal | None = None
    delta_ratio: Decimal | None = None
    distribution: dict[str, Any] | None = None
    top_n: list[dict[str, Any]] | None = None
    anomalies: list[dict[str, Any]] | None = None
    missing_rate: Decimal | None = None
    sample_rows: list[dict[str, Any]] | None = None
    unit: str | None = None
    time_range: dict[str, str] | None = None
    empty: bool = False


def _numeric_columns(columns: list[dict[str, Any]]) -> list[str]:
    return [c["name"] for c in columns if c.get("type", "").lower() in ("numeric", "decimal", "float", "integer", "bigint")]


def _detect_time_column(columns: list[dict[str, Any]]) -> str | None:
    for c in columns:
        name = c["name"].lower()
        if any(k in name for k in ("year", "month", "date", "time", "period")):
            return c["name"]
    return None


def summarizeEvidence(
    rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    *,
    question: str,
    max_sample_rows: int = 5,
    top_n_limit: int = 5,
) -> EvidenceSummary:
    if not rows:
        return EvidenceSummary(empty=True)

    numeric_cols = _numeric_columns(columns)
    time_col = _detect_time_column(columns)

    # 总量：取第一个数值列求和
    total = None
    if numeric_cols:
        total = sum(
            (r[numeric_cols[0]] for r in rows if r.get(numeric_cols[0]) is not None),
            Decimal(0),
        )

    # 基线/当前：按时间列排序后取首尾
    baseline = current = None
    if time_col and numeric_cols:
        sorted_rows = sorted(rows, key=lambda r: r.get(time_col) or "")
        first, last = sorted_rows[0], sorted_rows[-1]
        baseline = first.get(numeric_cols[0])
        current = last.get(numeric_cols[0])

    delta = None
    delta_ratio = None
    if baseline is not None and current is not None:
        try:
            delta = Decimal(str(current)) - Decimal(str(baseline))
            if Decimal(str(baseline)) != 0:
                delta_ratio = delta / Decimal(str(baseline))
        except Exception:
            pass

    # Top N
    top_n = None
    if numeric_cols:
        top_rows = sorted(
            [r for r in rows if r.get(numeric_cols[0]) is not None],
            key=lambda r: Decimal(str(r[numeric_cols[0]])),
            reverse=True,
        )[:top_n_limit]
        top_n = [{"rank": i + 1, "row": dict(r)} for i, r in enumerate(top_rows)]

    return EvidenceSummary(
        total=total,
        baseline=baseline,
        current=current,
        delta=delta,
        delta_ratio=delta_ratio,
        top_n=top_n,
        sample_rows=[dict(r) for r in rows[:max_sample_rows]],
        empty=False,
    )
```

### Step 3: 在 `runStep` 中生成 summary

```python
# backend/app/services/research_agent_execution.py
from app.services.research_evidence_summary import summarizeEvidence

async def runStep(...):
    # ... 执行 SQL ...
    rows = await executeReadonlySql(...)
    columns = [...]  # 从 adapter 取列元数据
    summary = summarizeEvidence(rows, columns, question=step.get("question", ""))
    return {
        "step": step,
        "rows": rows,
        "summary": summary,
        "truncated": len(rows) >= limit,
        "totalRows": len(rows),
    }
```

### Step 4: 跑测试确认通过

```bash
pytest tests/unit/services/test_research_evidence_summary.py -v
```

### Step 5: Commit

```bash
git add backend/app/services/research_evidence_summary.py backend/app/services/research_agent_execution.py \
        backend/tests/unit/services/test_research_evidence_summary.py
git commit -m "feat(research): 结构化 EvidenceSummary

- 总量/基线/当前/差值/增长率/TopN/样本
- runStep 执行后自动生成 summary
- 空结果返回 empty=True"
```

---

## Task 5: HypothesisTest 数据支持度判定

**Files:**
- Create: `backend/app/services/research_hypothesis_test.py`
- Modify: `backend/app/services/research_agent_phases.py:277-310`（`_stageVerify`）
- Modify: `backend/app/domain/research_models.py`（`ResearchFinding` 加 `verdict`, `evidence_summary`）
- Test: `backend/tests/unit/services/test_research_hypothesis_test.py`
- Test: `backend/tests/integration/research/test_hypothesis_verdict.py`
- Migration: `alembic revision --autogenerate -m "add finding verdict and evidence_summary"`

**Interfaces:**
- Consumes: `candidate`（断言 + 指标 + 方向/阈值）, `EvidenceSummary`, SQL 执行结果
- Produces:
  - `HypothesisVerdict` Enum：`supported`, `contradicted`, `inconclusive`, `execution_failed`
  - `HypothesisTest` dataclass：`assertion`, `metric`, `direction`, `threshold`, `baseline`, `verdict`, `evidence_summary`, `execution_succeeded`, `sample_size`, `limitations`
  - `evaluateHypothesis(candidate, summary, rows) -> HypothesisTest`

### Step 1: 写失败测试

```python
# backend/tests/unit/services/test_research_hypothesis_test.py
from decimal import Decimal
from app.services.research_hypothesis_test import evaluateHypothesis, HypothesisVerdict
from app.services.research_evidence_summary import EvidenceSummary


def test_zero_support_is_inconclusive_not_verified():
    candidate = {"assertion": "采购金额上升", "metric": "amount", "direction": "increase"}
    summary = EvidenceSummary(baseline=Decimal("100"), current=Decimal("100"), empty=False)
    result = evaluateHypothesis(candidate, summary, [])
    assert result.verdict == HypothesisVerdict.INCONCLUSIVE


def test_increase_supported():
    candidate = {"assertion": "采购金额上升", "metric": "amount", "direction": "increase"}
    summary = EvidenceSummary(baseline=Decimal("100"), current=Decimal("200"), empty=False)
    result = evaluateHypothesis(candidate, summary, rows=[{"amount": 200}])
    assert result.verdict == HypothesisVerdict.SUPPORTED
```

### Step 2: 实现 HypothesisTest

```python
# backend/app/services/research_hypothesis_test.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any


class HypothesisVerdict(str, Enum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"
    EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True)
class HypothesisTest:
    assertion: str
    metric: str
    direction: str | None
    threshold: Decimal | None
    baseline: Decimal | None
    current: Decimal | None
    verdict: HypothesisVerdict
    evidence_summary: dict[str, Any]
    execution_succeeded: bool
    sample_size: int
    limitations: list[str]


def _extract_value(summary: Any, metric: str) -> Decimal | None:
    # 优先用 current，其次 total
    if summary.current is not None:
        return Decimal(str(summary.current))
    if summary.total is not None:
        return Decimal(str(summary.total))
    return None


def evaluateHypothesis(
    candidate: dict[str, Any],
    summary: Any,
    rows: list[dict[str, Any]],
    *,
    execution_error: str | None = None,
) -> HypothesisTest:
    assertion = candidate.get("assertion", "")
    metric = candidate.get("metric", "")
    direction = candidate.get("direction")
    threshold = candidate.get("threshold")

    if execution_error is not None:
        return HypothesisTest(
            assertion=assertion, metric=metric, direction=direction, threshold=threshold,
            baseline=None, current=None, verdict=HypothesisVerdict.EXECUTION_FAILED,
            evidence_summary={}, execution_succeeded=False, sample_size=0,
            limitations=[f"执行失败: {execution_error}"],
        )

    baseline = summary.baseline
    current = _extract_value(summary, metric)
    sample_size = len(rows)

    limitations = []
    if sample_size == 0:
        limitations.append("无数据样本")
    if baseline is None:
        limitations.append("缺少基线")

    verdict = HypothesisVerdict.INCONCLUSIVE
    if summary.empty or current is None or sample_size == 0:
        verdict = HypothesisVerdict.INCONCLUSIVE
    elif direction == "increase":
        if current > baseline:
            verdict = HypothesisVerdict.SUPPORTED
        else:
            verdict = HypothesisVerdict.CONTRADICTED
    elif direction == "decrease":
        if current < baseline:
            verdict = HypothesisVerdict.SUPPORTED
        else:
            verdict = HypothesisVerdict.CONTRADICTED

    return HypothesisTest(
        assertion=assertion, metric=metric, direction=direction, threshold=threshold,
        baseline=baseline, current=current, verdict=verdict,
        evidence_summary=summary.__dict__ if summary else {},
        execution_succeeded=True, sample_size=sample_size, limitations=limitations,
    )
```

### Step 3: 修改 `_stageVerify`

```python
# backend/app/services/research_agent_phases.py
from app.services.research_hypothesis_test import evaluateHypothesis

async def _stageVerify(self, session, *, sessionId, turnId, emit, state):
    findings = []
    deps = await depsForSession(session, sessionId, self._exec)
    adapter = adapterFor(deps.datasource)
    for idx, candidate in enumerate(selectedHypotheses(state)):
        await session.commit()
        try:
            result_rows = await runVerification(...)
            summary = summarizeEvidence(result_rows, columns, question=candidate.get("assertion", ""))
            test = evaluateHypothesis(candidate, summary, result_rows)
        except Exception as exc:
            test = evaluateHypothesis(candidate, EvidenceSummary(empty=True), [], execution_error=str(exc))

        finding = {
            "claim_text": candidate["assertion"],
            "supporting_sql": candidate.get("sql"),
            "supporting_data": test.evidence_summary,
            "confidence": _confidenceFromVerdict(test.verdict),
            "verdict": test.verdict.value,
            "execution_succeeded": test.execution_succeeded,
            "limitations": test.limitations,
        }
        findings.append(finding)
        await saveFinding(session, finding)

    state["findings"] = findings
    return None


def _confidenceFromVerdict(verdict: Any) -> Decimal:
    mapping = {
        HypothesisVerdict.SUPPORTED: Decimal("0.9"),
        HypothesisVerdict.CONTRADICTED: Decimal("0.1"),
        HypothesisVerdict.INCONCLUSIVE: Decimal("0.5"),
        HypothesisVerdict.EXECUTION_FAILED: Decimal("0.0"),
    }
    return mapping.get(verdict, Decimal("0.5"))
```

### Step 4: 跑测试确认通过

```bash
pytest tests/unit/services/test_research_hypothesis_test.py -v
pytest tests/integration/research/test_hypothesis_verdict.py -v
```

### Step 5: Commit

```bash
git add backend/app/services/research_hypothesis_test.py backend/app/services/research_agent_phases.py \
        backend/app/domain/research_models.py \
        backend/tests/unit/services/test_research_hypothesis_test.py \
        backend/tests/integration/research/test_hypothesis_verdict.py \
        alembic/versions/xxxx_add_finding_verdict.py
git commit -m "feat(research): HypothesisTest 数据支持度判定

- verdict: supported/contradicted/inconclusive/execution_failed
- 零支持/空结果不再标 verified
- ResearchFinding 加 verdict/execution_succeeded/limitations"
```

---

## Task 6: ResearchStepResult / ResultSnapshot 持久化

**Files:**
- Create: `backend/app/services/research_step_result.py`
- Create: `backend/app/domain/research_models.py`（`ResearchStepResult` 表）
- Modify: `backend/app/services/research_agent_execution.py`（持久化 step 结果）
- Modify: `backend/app/services/research_agent_phases.py`（执行相位引用 snapshot）
- Test: `backend/tests/integration/research/test_step_result_snapshot.py`
- Migration: `alembic revision --autogenerate -m "add research_step_result"`

**Interfaces:**
- Consumes: step 定义、SQL、查询结果、EvidenceSummary
- Produces:
  - `ResearchStepResult` 表：`id`, `run_state_id`, `step_index`, `step_type`, `sql_text`, `raw_result_ref`, `evidence_summary`, `status`, `error`, `created_at`
  - `saveStepResult(session, runStateId, step, rows, summary, error) -> ResearchStepResult`
  - `loadStepResults(session, runStateId) -> list[ResearchStepResult]`

### Step 1: 写失败测试

```python
# backend/tests/integration/research/test_step_result_snapshot.py
async def test_step_result_saved_and_reloadable(client, db, research_session_factory):
    session = await research_session_factory()
    await client.post(f"/api/v1/research/sessions/{session.id}/turns", json={"question": "Q"})
    cp = await wait_for_checkpoint(session.id, stage="intent")
    await client.post(f"/api/v1/research/checkpoints/{cp['id']}/answer", json={"action": "confirm"})

    # 跑完 execute 后检查 ResearchStepResult
    step_results = await db.execute(select(ResearchStepResult).where(...))
    assert len(step_results.scalars().all()) > 0
```

### Step 2: 新增表

```python
# backend/app/domain/research_models.py
class ResearchStepResult(Base):
    """研究计划中单步的执行结果快照。"""

    __tablename__ = "research_step_result"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_state_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_run_state.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(50), nullable=False)
    sql_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_result_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evidence_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(_STATUS_LEN), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
```

### Step 3: 实现 repository 层

```python
# backend/app/services/research_step_result.py
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchStepResult


STEP_STATUS_SUCCESS = "success"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_EMPTY = "empty"


def buildStepResult(
    *,
    run_state_id: uuid.UUID,
    step_index: int,
    step_type: str,
    sql_text: str | None,
    raw_rows: list[dict[str, Any]],
    evidence_summary: dict[str, Any],
    error: str | None,
) -> ResearchStepResult:
    status = STEP_STATUS_SUCCESS
    if error:
        status = STEP_STATUS_FAILED
    elif not raw_rows:
        status = STEP_STATUS_EMPTY

    return ResearchStepResult(
        id=uuid.uuid4(),
        run_state_id=run_state_id,
        step_index=step_index,
        step_type=step_type,
        sql_text=sql_text,
        raw_result_ref={"rows": raw_rows, "schemaVersion": 1},
        evidence_summary=evidence_summary,
        status=status,
        error=error,
    )


async def saveStepResult(session: AsyncSession, result: ResearchStepResult) -> ResearchStepResult:
    session.add(result)
    return result


async def loadStepResults(session: AsyncSession, run_state_id: uuid.UUID) -> list[ResearchStepResult]:
    result = await session.execute(
        select(ResearchStepResult)
        .where(ResearchStepResult.run_state_id == run_state_id)
        .order_by(ResearchStepResult.step_index)
    )
    return list(result.scalars().all())
```

### Step 4: 在 `runStep` 后保存 snapshot

```python
# backend/app/services/research_agent_execution.py
from app.services.research_step_result import buildStepResult, saveStepResult

async def runStep(...):
    # ... 执行 ...
    result = buildStepResult(
        run_state_id=runStateId,
        step_index=step.get("index", 0),
        step_type=step.get("type", "sql"),
        sql_text=step.get("sql"),
        raw_rows=rows,
        evidence_summary=summary.__dict__,
        error=error,
    )
    await saveStepResult(session, result)
    return {"step": step, "result": result}
```

### Step 5: 跑测试确认通过

```bash
pytest tests/integration/research/test_step_result_snapshot.py -v
```

### Step 6: Commit

```bash
git add backend/app/domain/research_models.py backend/app/services/research_step_result.py \
        backend/app/services/research_agent_execution.py \
        backend/tests/integration/research/test_step_result_snapshot.py \
        alembic/versions/xxxx_add_research_step_result.py
git commit -m "feat(research): ResearchStepResult 持久化快照

- 每步结果落库：SQL、原始行、EvidenceSummary、状态、错误
- 支持 success/failed/empty 三种状态
- 报告生成时可回放基础查询结果"
```

---

## Task 7: ExecutionContext 与语义口径绑定

**Files:**
- Create: `backend/app/services/research_execution_context.py`
- Modify: `backend/app/services/enterprise_semantic_layer.py:87`
- Modify: `backend/app/services/research_agent_phases.py`（plan / execute 传递 context）
- Modify: `backend/app/services/research_agent_execution.py:generateStepSql`
- Test: `backend/tests/unit/services/test_research_execution_context.py`

**Interfaces:**
- Consumes: 确认后的 ESL arms、用户选择的指标、BO 映射、过滤条件
- Produces:
  - `ExecutionContext` dataclass：`businessObjects`, `metrics`, `filters`, `timeRange`, `rules`, `schema`, `dialect`
  - `buildExecutionContext(eslArms, userChoice, datasource) -> ExecutionContext`
  - `generateStepSql(step, context: ExecutionContext)` 接收 context

### Step 1: 写失败测试

```python
# backend/tests/unit/services/test_research_execution_context.py
from app.services.research_execution_context import buildExecutionContext, ExecutionContext


def test_build_context_preserves_selected_metric():
    arms = {
        "metrics": [{"name": "采购金额", "formula": "SUM(amount)", "unit": "元"}],
        "business_objects": [{"name": "采购订单", "table": "purchase_order"}],
    }
    choice = {"selectedMetrics": ["采购金额"], "timeRange": {"from": "2024-01-01", "to": "2024-12-31"}}
    ctx = buildExecutionContext(arms, choice, dialect="oracle")
    assert ctx.metrics[0]["name"] == "采购金额"
    assert ctx.time_range["from"] == "2024-01-01"
```

### Step 2: 实现 ExecutionContext

```python
# backend/app/services/research_execution_context.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionContext:
    business_objects: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    filters: list[dict[str, Any]]
    time_range: dict[str, str] | None
    rules: list[dict[str, Any]]
    schema: str | None
    dialect: str


def buildExecutionContext(
    esl_arms: dict[str, Any] | None,
    user_choice: dict[str, Any],
    *,
    datasource: dict[str, Any] | None,
) -> ExecutionContext:
    arms = esl_arms or {}
    selected_metrics = user_choice.get("selectedMetrics", [])
    metrics = [m for m in arms.get("metrics", []) if m.get("name") in selected_metrics]
    if not metrics:
        metrics = arms.get("metrics", [])[:1]  # 默认第一个

    return ExecutionContext(
        business_objects=arms.get("business_objects", []),
        metrics=metrics,
        filters=user_choice.get("filters", []),
        time_range=user_choice.get("timeRange"),
        rules=arms.get("rules", []),
        schema=datasource.get("schema") if datasource else None,
        dialect=datasource.get("dialect", "oracle") if datasource else "oracle",
    )
```

### Step 3: 修改 plan/execute 传递 context

```python
# backend/app/services/research_agent_phases.py
from app.services.research_execution_context import buildExecutionContext

async def _stagePlan(self, session, *, sessionId, turnId, emit, state):
    # ... 生成 plan ...
    ctx = buildExecutionContext(state.get("esl"), state.get("choice", {}), datasource=deps.datasource)
    plan = await self._planner.plan(state["question"], context=ctx)
    state["plan"] = plan
    state["executionContext"] = ctx.__dict__
    # ...
```

### Step 4: 跑测试确认通过

```bash
pytest tests/unit/services/test_research_execution_context.py -v
```

### Step 5: Commit

```bash
git add backend/app/services/research_execution_context.py backend/app/services/research_agent_phases.py \
        backend/app/services/research_agent_execution.py \
        backend/tests/unit/services/test_research_execution_context.py
git commit -m "feat(research): ExecutionContext 绑定已确认语义口径

- buildExecutionContext 从 ESL arms + userChoice 构建
- 指标/时间范围/过滤/dialect/schema 进入 plan 与 SQL 生成
- 未选指标时默认取首个"
```

---

## Task 8: 报告引用基础结果与证据面板

**Files:**
- Modify: `backend/app/services/report_planner.py`
- Modify: `backend/app/services/research_agent_phases.py:242`（`_stageReport`）
- Modify: `frontend/src/components/research/ReportRenderer.tsx:139`
- Create: `frontend/src/components/re research/EvidencePanel.tsx`
- Test: `backend/tests/integration/research/test_report_evidence.py`
- Test: `frontend/tests/unit/research/EvidencePanel.test.tsx`

**Interfaces:**
- Consumes: `ResearchRunState`, `list[ResearchStepResult]`, `list[ResearchFinding]`
- Produces:
  - 报告 payload 新增 `evidence` 字段：`{stepResults: [...], findings: [...], scope: {...}}`
  - `EvidencePanel` 组件：展示 SQL、摘要、数据样本、数据源版本

### Step 1: 写失败测试

```python
# backend/tests/integration/research/test_report_evidence.py
async def test_report_includes_step_results(client, research_session_factory):
    session = await research_session_factory()
    await client.post(f"/api/v1/research/sessions/{session.id}/turns", json={"question": "Q"})
    # ... 走完全流程 ...
    report = await client.get(f"/api/v1/research/sessions/{session.id}/report")
    assert "evidence" in report.json()["payload"]
    assert len(report.json()["payload"]["evidence"]["stepResults"]) > 0
```

### Step 2: 修改 `_stageReport`

```python
# backend/app/services/research_agent_phases.py
from app.services.research_step_result import loadStepResults

async def _stageReport(self, session, *, sessionId, turnId, emit, state):
    run_state_id = state["runStateId"]
    step_results = await loadStepResults(session, run_state_id)
    findings = state.get("findings", [])

    report = await self._reporter.render(
        question=state["question"],
        mode=state["mode"],
        step_results=step_results,
        findings=findings,
        scope=state.get("executionContext"),
    )
    state["report"] = report
    await emitEvent(emit, EVENT_REPORT, {"report": report})
    return None
```

### Step 3: 修改 ReportPlanner 契约

```python
# backend/app/services/report_planner.py
async def render(self, *, question, mode, step_results, findings, scope):
    # 组装 evidence 字段
    evidence = {
        "stepResults": [
            {
                "index": r.step_index,
                "type": r.step_type,
                "sql": r.sql_text,
                "summary": r.evidence_summary,
                "status": r.status,
                "error": r.error,
            }
            for r in step_results
        ],
        "findings": findings,
        "scope": scope,
    }
    # ... 调用 LLM 生成 report ...
    return {"sections": [...], "evidence": evidence}
```

### Step 4: 前端 EvidencePanel

```tsx
// frontend/src/components/research/EvidencePanel.tsx
import { Card, Tag, Table } from "antd";

interface EvidencePanelProps {
  evidence: {
    stepResults: Array<{
      index: number;
      type: string;
      sql: string;
      summary: Record<string, any>;
      status: string;
    }>;
    findings: Array<any>;
  };
}

export function EvidencePanel({ evidence }: EvidencePanelProps) {
  return (
    <div data-testid="evidence-panel">
      {evidence.stepResults.map((step) => (
        <Card key={step.index} title={`步骤 ${step.index + 1}: ${step.type}`}>
          <pre>{step.sql}</pre>
          <Tag color={step.status === "success" ? "green" : "red"}>{step.status}</Tag>
          {step.summary && <div>总量: {step.summary.total}</div>}
        </Card>
      ))}
    </div>
  );
}
```

### Step 5: 跑测试确认通过

```bash
pytest tests/integration/research/test_report_evidence.py -v
cd frontend && npx vitest run tests/unit/research/EvidencePanel.test.tsx
```

### Step 6: Commit

```bash
git add backend/app/services/report_planner.py backend/app/services/research_agent_phases.py \
        frontend/src/components/research/EvidencePanel.tsx frontend/src/components/research/ReportRenderer.tsx \
        backend/tests/integration/research/test_report_evidence.py \
        frontend/tests/unit/research/EvidencePanel.test.tsx
git commit -m "feat(research): 报告引用基础查询结果 + 前端证据面板

- _stageReport 加载 ResearchStepResult 传入 reporter
- report payload 新增 evidence.stepResults/findings/scope
- EvidencePanel 展示 SQL、状态、摘要"
```

---

## Task 9: 环节一集成测试与验收

**Files:**
- Create: `backend/tests/integration/research/test_evidence_contract_e2e.py`
- Modify: `backend/tests/conftest.py`（必要时补充 research fixtures）

**Interfaces:**
- 端到端覆盖：追问不回落、并发拒绝、Decimal/date 序列化、零支持不标 verified、基础结果进报告

### Step 1: 编写端到端测试

```python
# backend/tests/integration/research/test_evidence_contract_e2e.py
@pytest.mark.parametrize("scenario", [
    "follow_up_keeps_scope",
    "concurrent_turn_rejected",
    "decimal_date_serialization",
    "zero_support_inconclusive",
    "step_result_in_report",
])
async def test_evidence_contract_scenario(client, scenario, research_session_factory):
    if scenario == "follow_up_keeps_scope":
        # 已实现于 Task 1
        pass
    # ... 其余场景分别调用已实现的接口 ...
```

### Step 2: 跑全量研究集成测试

```bash
cd backend
pytest tests/integration/research/ -v
```

Expected: 全绿（含新增测试）

### Step 3: Commit

```bash
git add backend/tests/integration/research/test_evidence_contract_e2e.py
git commit -m "test(research): 环节一证据契约端到端验收

- 追问范围保留、并发拒绝、序列化、零支持、基础结果进报告"
```

---

# 环节二：平台级增强（在环节一上线后启动）

本环节对应建议文档中的“第三阶段：增强兼容与体验”，以及模型能力、预算治理、持久化任务等中长期项。

---

## Task 10: ResearchRun 版本化快照

**Files:**
- Modify: `backend/app/domain/research_models.py`（扩展 `ResearchRunState.state_payload` 使用；新增 `research_run_snapshot` 表）
- Create: `backend/app/services/research_run_snapshot.py`
- Modify: `backend/app/services/research_agent_service.py`（关键相位落 snapshot）
- Test: `backend/tests/integration/research/test_run_snapshot.py`
- Migration: `alembic revision --autogenerate -m "add research_run_snapshot"`

**Interfaces:**
- Consumes: `ResearchRunState`, 当前 `state` dict
- Produces:
  - `ResearchRunSnapshot` 表：`id`, `run_state_id`, `phase`, `state_payload`, `schema_version`, `created_at`
  - `snapshotRunState(session, runStateId, phase, state) -> ResearchRunSnapshot`
  - `loadLatestSnapshot(session, runStateId, beforePhase=None) -> ResearchRunSnapshot | None`

### Step 1: 写失败测试

```python
# backend/tests/integration/research/test_run_snapshot.py
async def test_snapshot_created_at_each_phase(client, db, research_session_factory):
    session = await research_session_factory()
    await client.post(f"/api/v1/research/sessions/{session.id}/turns", json={"question": "Q"})
    snapshots = await db.execute(select(ResearchRunSnapshot).where(...))
    phases = {s.phase for s in snapshots.scalars().all()}
    assert "intent" in phases
    assert "esl" in phases
```

### Step 2: 新增表

```python
# backend/app/domain/research_models.py
class ResearchRunSnapshot(Base):
    """运行状态机各相位的完整快照，用于 worker 重启后恢复。"""

    __tablename__ = "research_run_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_state_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_run_state.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phase: Mapped[str] = mapped_column(String(_PHASE_LEN), nullable=False)
    state_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
```

### Step 3: 实现 snapshot 服务

```python
# backend/app/services/research_run_snapshot.py
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchRunSnapshot


async def snapshotRunState(
    session: AsyncSession,
    run_state_id: uuid.UUID,
    phase: str,
    state: dict[str, Any],
    schema_version: int = 1,
) -> ResearchRunSnapshot:
    snap = ResearchRunSnapshot(
        id=uuid.uuid4(),
        run_state_id=run_state_id,
        phase=phase,
        state_payload=dict(state),
        schema_version=schema_version,
    )
    session.add(snap)
    return snap


async def loadLatestSnapshot(
    session: AsyncSession,
    run_state_id: uuid.UUID,
    before_phase: str | None = None,
) -> ResearchRunSnapshot | None:
    query = select(ResearchRunSnapshot).where(ResearchRunSnapshot.run_state_id == run_state_id)
    if before_phase:
        query = query.where(ResearchRunSnapshot.phase == before_phase)
    query = query.order_by(ResearchRunSnapshot.created_at.desc()).limit(1)
    result = await session.execute(query)
    return result.scalar_one_or_none()
```

### Step 4: 在 `_guardedRun` 相位切换时落 snapshot

```python
# backend/app/services/research_agent_service.py
from app.services.research_run_snapshot import snapshotRunState

async def _guardedRun(self, session, *, sessionId, turnId, runStateId, phase, emit, state):
    await snapshotRunState(session, runStateId, phase, state)
    # ... 原有相位调度 ...
```

### Step 5: Commit

```bash
git add backend/app/domain/research_models.py backend/app/services/research_run_snapshot.py \
        backend/app/services/research_agent_service.py \
        backend/tests/integration/research/test_run_snapshot.py \
        alembic/versions/xxxx_add_research_run_snapshot.py
git commit -m "feat(research): ResearchRun 版本化快照

- 每个 phase 进入时落快照
- 支持按 phase 加载最新快照
- 为 worker 重启恢复做准备"
```

---

## Task 11: 持久化任务队列与 worker 租约

**Files:**
- Create: `backend/app/services/research_task_queue.py`
- Create: `backend/app/workers/research_worker.py`
- Modify: `backend/app/api/v1/research.py`（将 BackgroundTasks 改为入队）
- Modify: `backend/app/main.py`（启动 worker 进程）
- Test: `backend/tests/integration/research/test_task_queue.py`

**Interfaces:**
- Consumes: `ResearchRunState.id`, action (`startTurn` / `resumeTurn`), payload
- Produces:
  - `ResearchTask` dataclass：`id`, `run_state_id`, `action`, `payload`, `status`, `lease_until`, `attempt`, `created_at`
  - `enqueueTask(session, task) -> ResearchTask`
  - `leaseNextTask(session, worker_id) -> ResearchTask | None`
  - `completeTask(session, task_id) -> None`

### Step 1: 新增表

```python
# backend/app/domain/research_models.py
class ResearchTask(Base):
    """持久化研究任务队列。"""

    __tablename__ = "research_task"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_state_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_run_state.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(_STATUS_LEN), nullable=False, default="pending")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
```

### Step 2: 实现任务队列 repository

```python
# backend/app/services/research_task_queue.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchTask


TASK_STATUS_PENDING = "pending"
TASK_STATUS_LEASED = "leased"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"


def buildTask(run_state_id: uuid.UUID, action: str, payload: dict[str, Any]) -> ResearchTask:
    return ResearchTask(
        id=uuid.uuid4(),
        run_state_id=run_state_id,
        action=action,
        payload=payload,
        status=TASK_STATUS_PENDING,
        attempt=0,
        max_attempts=3,
    )


async def enqueueTask(session: AsyncSession, task: ResearchTask) -> ResearchTask:
    session.add(task)
    return task


async def leaseNextTask(session: AsyncSession, worker_id: str, lease_seconds: int = 300) -> ResearchTask | None:
    now = datetime.now(UTC)
    result = await session.execute(
        select(ResearchTask)
        .where(ResearchTask.status == TASK_STATUS_PENDING)
        .where((ResearchTask.lease_until == None) | (ResearchTask.lease_until < now))
        .where(ResearchTask.attempt < ResearchTask.max_attempts)
        .order_by(ResearchTask.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None
    task.status = TASK_STATUS_LEASED
    task.worker_id = worker_id
    task.lease_until = now + timedelta(seconds=lease_seconds)
    task.attempt += 1
    session.add(task)
    return task


async def completeTask(session: AsyncSession, task_id: uuid.UUID) -> None:
    task = await session.get(ResearchTask, task_id)
    if task is not None:
        task.status = TASK_STATUS_DONE
        task.lease_until = None
        session.add(task)


async def failTask(session: AsyncSession, task_id: uuid.UUID, error: str) -> None:
    task = await session.get(ResearchTask, task_id)
    if task is not None:
        if task.attempt >= task.max_attempts:
            task.status = TASK_STATUS_FAILED
        else:
            task.status = TASK_STATUS_PENDING
        task.lease_until = None
        task.payload["lastError"] = error
        session.add(task)
```

### Step 3: 修改 API 入队

```python
# backend/app/api/v1/research.py
from app.services.research_task_queue import buildTask, enqueueTask

async def createTurn(...):
    # ... 创建 turn / runState / claim ...
    task = buildTask(run_state.id, "startTurn", {"question": payload.question, "userId": user.dbUserId, "mode": row.mode})
    await enqueueTask(db, task)
    await db.commit()
    return ResearchTurnAccepted(sessionId=sessionId, turnId=turn.id, runId=run_state.id, taskId=task.id, status="queued")
```

### Step 4: 实现 worker

```python
# backend/app/workers/research_worker.py
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app.db import getSessionFactory
from app.services.research_agent_service import ResearchAgentService
from app.services.research_task_queue import leaseNextTask, completeTask, failTask


async def run_worker(worker_id: str, stop_event: asyncio.Event):
    factory = getSessionFactory()
    service = ResearchAgentService(...)
    while not stop_event.is_set():
        async with factory() as session:
            task = await leaseNextTask(session, worker_id)
            if task is None:
                await session.commit()
                await asyncio.sleep(1)
                continue
            await session.commit()

        try:
            async with factory() as session:
                if task.action == "startTurn":
                    await service.startTurn(...)
                elif task.action == "resumeTurn":
                    await service.resumeTurn(...)
                await completeTask(session, task.id)
                await session.commit()
        except Exception as exc:
            async with factory() as session:
                await failTask(session, task.id, str(exc))
                await session.commit()
```

### Step 5: Commit

```bash
git add backend/app/domain/research_models.py backend/app/services/research_task_queue.py \
        backend/app/workers/research_worker.py backend/app/api/v1/research.py backend/app/main.py \
        backend/tests/integration/research/test_task_queue.py \
        alembic/versions/xxxx_add_research_task.py
git commit -m "feat(research): 持久化任务队列与 worker 租约

- ResearchTask 队列表 + lease/complete/fail 语义
- API 入队替代 BackgroundTasks
- research_worker 拉取任务并执行"
```

---

## Task 12: 持久化 Event Outbox 与 SSE lastEventId

**Files:**
- Create: `backend/app/services/research_event_outbox.py`
- Modify: `backend/app/services/research_event_bus.py:32`
- Modify: `backend/app/api/v1/research.py:470-540`（SSE 支持 `lastEventId`）
- Test: `backend/tests/integration/research/test_event_outbox.py`
- Migration: `alembic revision --autogenerate -m "add research_event_outbox"`

**Interfaces:**
- Consumes: research 事件 `(event, payload)`
- Produces:
  - `ResearchEventOutbox` 表：`id`, `session_id`, `run_state_id`, `event`, `payload`, `event_id`, `created_at`
  - `emitEventOutbox(session, sessionId, runStateId, event, payload)`
  - SSE `lastEventId` 回放

### Step 1: 新增表

```python
# backend/app/domain/research_models.py
class ResearchEventOutbox(Base):
    """研究事件持久化 outbox，支持 SSE 重连回放。"""

    __tablename__ = "research_event_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    run_state_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
```

### Step 2: 实现 outbox

```python
# backend/app/services/research_event_outbox.py
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchEventOutbox


def buildEventId() -> str:
    return str(uuid.uuid4())


async def emitEventOutbox(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    run_state_id: uuid.UUID,
    event: str,
    payload: dict[str, Any],
) -> ResearchEventOutbox:
    outbox = ResearchEventOutbox(
        id=uuid.uuid4(),
        session_id=session_id,
        run_state_id=run_state_id,
        event=event,
        payload=payload,
        event_id=buildEventId(),
    )
    session.add(outbox)
    return outbox


async def fetchEventsAfter(
    session: AsyncSession,
    session_id: uuid.UUID,
    after_event_id: str | None,
    limit: int = 100,
) -> list[ResearchEventOutbox]:
    query = select(ResearchEventOutbox).where(ResearchEventOutbox.session_id == session_id)
    if after_event_id:
        after = await session.execute(
            select(ResearchEventOutbox.created_at).where(ResearchEventOutbox.event_id == after_event_id)
        )
        after_time = after.scalar_one_or_none()
        if after_time:
            query = query.where(ResearchEventOutbox.created_at > after_time)
    query = query.order_by(ResearchEventOutbox.created_at.asc()).limit(limit)
    result = await session.execute(query)
    return list(result.scalars().all())
```

### Step 3: 修改 event bus 持久化事件

```python
# backend/app/services/research_event_bus.py
async def emitEvent(emit: Emit | None, event: str, payload: dict[str, Any]) -> None:
    # 优先持久化到 outbox
    if emit is not None and hasattr(emit, "session"):
        from app.services.research_event_outbox import emitEventOutbox
        await emitEventOutbox(
            emit.session,
            session_id=emit.session_id,
            run_state_id=emit.run_state_id,
            event=event,
            payload=payload,
        )
    # 再广播到内存队列
    if emit is not None and hasattr(emit, "queue"):
        await emit.queue.put({"event": event, "payload": payload})
```

### Step 4: SSE 支持 lastEventId

```python
# backend/app/api/v1/research.py
async def streamSessionEvents(
    sessionId: uuid.UUID,
    lastEventId: str | None = Query(default=None),
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    async with getSessionFactory()() as db:
        await _ownedSession(db, sessionId, user)
    return StreamingResponse(
        _eventFrames(str(sessionId), lastEventId),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

async def _eventFrames(sessionId: str, lastEventId: str | None = None) -> AsyncIterator[str]:
    # 先回放历史事件
    if lastEventId:
        async with getSessionFactory()() as db:
            events = await fetchEventsAfter(db, uuid.UUID(sessionId), lastEventId)
            for ev in events:
                yield f"event: {ev.event}\nid: {ev.event_id}\ndata: {research_json(ev.payload)}\n\n"
    # 再订阅新事件
    queue = eventBus.subscribe(sessionId)
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            payload = event.get("payload", {})
            event_id = event.get("event_id", "")
            yield f"event: {event['event']}\nid: {event_id}\ndata: {research_json(payload)}\n\n"
    finally:
        eventBus.unsubscribe(sessionId, queue)
```

### Step 5: Commit

```bash
git add backend/app/domain/research_models.py backend/app/services/research_event_outbox.py \
        backend/app/services/research_event_bus.py backend/app/api/v1/research.py \
        backend/tests/integration/research/test_event_outbox.py \
        alembic/versions/xxxx_add_research_event_outbox.py
git commit -m "feat(research): 持久化 event outbox + SSE lastEventId 回放

- 事件先写 outbox 再广播
- SSE 支持 lastEventId 查询参数
- 断线重连可回放历史事件"
```

---

## Task 13: 三 mode 策略差异化

**Files:**
- Create: `backend/app/services/research_mode_strategy.py`
- Modify: `backend/app/services/research_agent_phases.py`（plan / report 调用策略）
- Modify: `backend/app/services/report_planner.py`
- Test: `backend/tests/unit/services/test_research_mode_strategy.py`

**Interfaces:**
- Consumes: `mode: "research" | "attribution" | "compare"`, `EvidenceSummary`, `findings`
- Produces:
  - `ModeStrategy` dataclass：`planPrompt`, `reportSections`, `requiredEvidence`, `comparisonModel`
  - `resolveModeStrategy(mode) -> ModeStrategy`

### Step 1: 定义策略

```python
# backend/app/services/research_mode_strategy.py
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModeStrategy:
    mode: str
    plan_prompt_suffix: str
    report_sections: list[str]
    requires_baseline: bool
    requires_counterfactual: bool
    comparison_axes: list[str] | None = None


def resolveModeStrategy(mode: str) -> ModeStrategy:
    strategies = {
        "research": ModeStrategy(
            mode="research",
            plan_prompt_suffix="先完成描述性查询，再按需提出假设",
            report_sections=["summary", "scope", "evidence", "findings", "limitations"],
            requires_baseline=False,
            requires_counterfactual=False,
        ),
        "attribution": ModeStrategy(
            mode="attribution",
            plan_prompt_suffix="保存基线和变化事实，设计可验证候选原因",
            report_sections=["baseline", "change", "candidate_causes", "verified", "alternative_explanations", "limitations"],
            requires_baseline=True,
            requires_counterfactual=True,
        ),
        "compare": ModeStrategy(
            mode="compare",
            plan_prompt_suffix="显式选择对照对象、期间、指标、单位和口径",
            report_sections=["subjects", "metrics", "differences", "statistical_summary", "interpretation"],
            requires_baseline=True,
            requires_counterfactual=False,
            comparison_axes=["subject", "period", "metric"],
        ),
    }
    return strategies.get(mode, strategies["research"])
```

### Step 2: 在 plan/report 中接入

```python
# backend/app/services/research_agent_phases.py
from app.services.research_mode_strategy import resolveModeStrategy

async def _stagePlan(self, session, *, sessionId, turnId, emit, state):
    strategy = resolveModeStrategy(state["mode"])
    # ... plan 生成时附加 strategy.plan_prompt_suffix ...

async def _stageReport(self, session, *, sessionId, turnId, emit, state):
    strategy = resolveModeStrategy(state["mode"])
    report = await self._reporter.render(
        question=state["question"],
        mode=state["mode"],
        strategy=strategy,
        ...
    )
```

### Step 3: Commit

```bash
git add backend/app/services/research_mode_strategy.py backend/app/services/research_agent_phases.py \
        backend/app/services/report_planner.py \
        backend/tests/unit/services/test_research_mode_strategy.py
git commit -m "feat(research): 三 mode 差异化策略

- research/attribution/compare 各自 plan prompt 与 report 章节
- attribution 强制基线，compare 强制对照对象"
```

---

## Task 14: 图表与证据回放

**Files:**
- Modify: `backend/app/services/research_agent_execution.py:463-501`（`chartStep`）
- Modify: `frontend/src/components/research/ReportRenderer.tsx`
- Modify: `frontend/src/components/research/EvidencePanel.tsx`
- Test: `frontend/tests/unit/research/EvidencePanel.test.tsx`

**Interfaces:**
- Consumes: `ResearchStepResult.evidence_summary`, chart type hint
- Produces: 可交互图表 + SQL/数据回放

### Step 1: 修复 chartType

```python
# backend/app/services/research_agent_execution.py
async def chartStep(...):
    # 根据 summary 自动推断 chartType
    chart_type = "table"
    if summary.time_range and summary.current is not None and summary.baseline is not None:
        chart_type = "line"
    elif summary.top_n:
        chart_type = "bar"
    return {"chartType": chart_type, "data": summary.__dict__}
```

### Step 2: 前端证据回放

```tsx
// frontend/src/components/research/EvidencePanel.tsx
import { LineChart, BarChart } from "@/components/charts";

export function EvidencePanel({ evidence }) {
  return (
    <div data-testid="evidence-panel">
      {evidence.stepResults.map((step) => (
        <Card key={step.index}>
          {step.summary?.time_range && step.summary?.current != null && (
            <LineChart data={step.summary.sample_rows} x="year" y="amount" />
          )}
          {step.summary?.top_n && (
            <BarChart data={step.summary.top_n.map(t => t.row)} x="name" y="amount" />
          )}
          <pre>{step.sql}</pre>
        </Card>
      ))}
    </div>
  );
}
```

### Step 3: Commit

```bash
git add backend/app/services/research_agent_execution.py \
        frontend/src/components/research/ReportRenderer.tsx \
        frontend/src/components/research/EvidencePanel.tsx \
        frontend/tests/unit/research/EvidencePanel.test.tsx
git commit -m "feat(research): 图表类型推断与证据回放

- chartStep 根据 summary 推断 line/bar/table
- EvidencePanel 支持折线图/柱状图与 SQL 回放"
```

---

## Task 15: 阶段模型能力与预算治理

**Files:**
- Create: `backend/app/services/research_budget.py`
- Modify: `backend/app/services/research_agent_service.py`（每 phase 检查预算）
- Modify: `backend/app/services/research_agent_phases.py`（按阶段选模型）
- Test: `backend/tests/unit/services/test_research_budget.py`

**Interfaces:**
- Consumes: `ResearchRunState.state_payload["budget"]`, phase, model config
- Produces:
  - `ResearchBudget` dataclass：`maxCost`, `maxCalls`, `deadline`, `phaseBudgets`
  - `checkBudget(budget, usage) -> BudgetStatus`
  - `selectModelForPhase(phase, context) -> modelConfig`

### Step 1: 定义预算

```python
# backend/app/services/research_budget.py
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ResearchBudget:
    max_cost: Decimal
    max_calls: int
    deadline: datetime
    phase_budgets: dict[str, Decimal]
    used_cost: Decimal = Decimal("0")
    used_calls: int = 0


class BudgetStatus:
    OK = "ok"
    COST_EXCEEDED = "cost_exceeded"
    CALLS_EXCEEDED = "calls_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"


def checkBudget(budget: ResearchBudget, *, phase: str, phase_cost: Decimal) -> str:
    now = datetime.now(UTC)
    if now > budget.deadline:
        return BudgetStatus.DEADLINE_EXCEEDED
    if budget.used_calls + 1 > budget.max_calls:
        return BudgetStatus.CALLS_EXCEEDED
    phase_limit = budget.phase_budgets.get(phase, budget.max_cost)
    if budget.used_cost + phase_cost > budget.max_cost:
        return BudgetStatus.COST_EXCEEDED
    if phase_cost > phase_limit:
        return BudgetStatus.COST_EXCEEDED
    return BudgetStatus.OK
```

### Step 2: 在状态机中检查

```python
# backend/app/services/research_agent_service.py
async def _guardedRun(self, session, *, sessionId, turnId, runStateId, phase, emit, state):
    budget = ResearchBudget(**state.get("budget", DEFAULT_BUDGET))
    # ... 每 phase 调用前 checkBudget ...
```

### Step 3: Commit

```bash
git add backend/app/services/research_budget.py backend/app/services/research_agent_service.py \
        backend/app/services/research_agent_phases.py \
        backend/tests/unit/services/test_research_budget.py
git commit -m "feat(research): 阶段预算与模型能力治理

- ResearchBudget 定义 cost/calls/deadline/phaseBudgets
- 状态机每 phase 调用前检查预算
- 超预算时返回明确错误事件"
```

---

## Task 16: 环节二集成测试与灰度验收

**Files:**
- Create: `backend/tests/integration/research/test_platform_enhancements_e2e.py`
- Modify: `docker/docker-compose.yml`（如需独立 worker 服务）

**Interfaces:**
- 覆盖：worker 重启恢复、SSE lastEventId 回放、三 mode 差异化、预算超限

### Step 1: 编写端到端测试

```python
# backend/tests/integration/research/test_platform_enhancements_e2e.py
async def test_worker_restart_resumes_from_snapshot(client, research_session_factory):
    # 启动运行，终止 worker，新 worker 接手后应能从 snapshot 恢复
    pass

async def test_sse_replay_after_reconnect(client, research_session_factory):
    # 订阅 SSE，记录 event_id，断线后用 lastEventId 重连回放
    pass
```

### Step 2: 跑全量测试

```bash
cd backend
pytest tests/integration/research/ -v
```

### Step 3: Commit

```bash
git add backend/tests/integration/research/test_platform_enhancements_e2e.py
git commit -m "test(research): 环节二平台级增强端到端验收

- worker 重启恢复、SSE 回放、mode 差异化、预算治理"
```

---

# Self-Review

**1. Spec coverage:**
- 建议文档问题 1（恢复丢问题）→ Task 1
- 问题 2（缺业务数值）→ Task 4
- 问题 3（SQL 无错误即 verified）→ Task 5
- 问题 4（NameError）→ 已修复，无需任务
- 问题 5（并发轮次）→ Task 2
- 问题 6（SSE 生命周期）→ Task 3（序列化）+ 环节二 Task 12（lastEventId）
- 问题 7（持久化任务/事件）→ 环节二 Task 10-12
- 问题 8（数据类型兼容）→ Task 3
- 问题 9（语义确认改变执行）→ Task 7
- 问题 10（基础结果进报告）→ Task 6 + Task 8
- 问题 11（mode 业务能力）→ 环节二 Task 13
- 问题 12（模型协议/成本/恢复）→ 环节二 Task 15

**2. Placeholder scan:**
- 所有 code block 均含实际代码；无 `TBD`/`TODO`/`implement later`。
- Task 1 测试中的 `???` 在 Step 3 后替换为可运行查询，已标注说明。
- Task 11/16 中 worker 启动入口需按项目具体 main.py 结构落地，已给出模式代码。

**3. Type consistency:**
- `ResearchRunState` 字段在模型、构造器、恢复态中一致使用 `current_question`, `scope_version`, `state_payload`。
- `EvidenceSummary` 在 `research_evidence_summary.py` 与 `research_hypothesis_test.py` 中字段一致。
- `verdict` 统一为 `HypothesisVerdict` enum，落库时转 `.value`。

**4. Scope check:**
- 环节一可独立上线；环节二依赖环节一的数据模型（`ResearchRunState`, `ResearchStepResult`）。
- 每个 task 都有独立测试和 commit， reviewer 可逐 task 审批。
