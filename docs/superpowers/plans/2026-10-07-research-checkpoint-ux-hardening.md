# 研究分析检查点 UX 加固与可靠性收口 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 解决「确认/修改/拒绝」三按钮无语义说明的用户报障，并收口深度审计发现的研究检查点可靠性问题（modify 无限循环、checkpoint 无 TTL、并发提交 500、SSE 重连时断、audit_log 缺失）。

**Architecture:**
- 前端：i18n 加 hint 键 + CheckpointCard 加 tooltip/上下文说明，按 checkpoint phase 上下文化
- 后端：给 ResearchCheckpoint 加 `expires_at` + resolve 时 staleness 校验；加 max-replan 计数器；并发提交改 409；answer 提交即落 audit_log
- 测试：每条任务 RED→GREEN，集成测试用真实 PG；前端用 vitest + RTL

**Tech Stack:**
- 后端：Python 3.11 / FastAPI / SQLAlchemy 2 async / Pydantic v2 / pytest-asyncio / alembic
- 前端：React 18 + TypeScript + antd + vitest + @testing-library/react
- 数据库：PostgreSQL（测试端口 5434，集成测试必须真实 DB）

## Global Constraints

- 项目根：`/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system`
- 工作目录：后端 `cd backend && ...`；前端 `cd frontend && ...`
- 文件大小上限 800 行（`backend/app/services/chat_multistep.py` 已豁免至 947）
- 函数上限 50 行，嵌套 ≤ 4 层
- 不可变数据模式：创建新对象，不原地修改
- 真实 PG 测试库（端口 5434，库 `qa_metadata_test`），禁止 sqlite 内存库替代
- ORM/Pydantic 字段：`snake_case`；前端 TS 变量：`camelCase`
- 命名常量：`UPPER_SNAKE_CASE`
- 提交规范：`refactor(...)`、`feat(...)`、`fix(...)`、`test(...)`、`docs(...)` 等 conventional commit
- TDD 强制：写测试（RED）→ 实现（GREEN）→ 重构（IMPROVE），覆盖率 ≥ 80%
- 测试文件命名：后端 `backend/app/tests/{unit,integration}/test_<name>.py`；前端 `frontend/src/tests/<Name>.test.tsx`
- DB 迁移：所有 schema 变更走 alembic，迁移名带日期前缀
- SSE 协议：`bus.publish(sessionId, event)`，事件名走 `research_agent_ports.EVENT_*` 常量

---

## Phase A — 检查点 UX（直接解决用户报障）

### Task A1: i18n 加 6 个 hint 键（中英双语）

**Files:**
- Modify: `frontend/src/i18n/zh-CN.ts:2938-2940`（在 `checkpoint` 块内追加 6 键）
- Modify: `frontend/src/i18n/en-US.ts`（同步 6 键英文版）
- Test: `frontend/src/i18n/i18n.test.ts`（已有；扩 case 覆盖新键）

**Step A1.1: 写失败测试**

```typescript
// frontend/src/i18n/i18n.test.ts
import { zhCN } from "./zh-CN";
import { enUS } from "./en-US";

describe("research.checkpoint hints", () => {
  const REQUIRED_KEYS = [
    "confirmHint",
    "modifyHint",
    "rejectHint",
    "modifyPlanningHint",
    "rejectAbortHint",
    "hypothesisConfirmHint",
  ] as const;

  for (const key of REQUIRED_KEYS) {
    it(`zh-CN has research.checkpoint.${key}`, () => {
      expect(zhCN.research.checkpoint[key]).toBeTruthy();
    });
    it(`en-US has research.checkpoint.${key}`, () => {
      expect(enUS.research.checkpoint[key]).toBeTruthy();
    });
  }
});
```

**Step A1.2: 跑测试 → RED**

Run: `cd frontend && npx vitest run src/i18n/i18n.test.ts`
Expected: 12 个 case 全部失败（缺键）

**Step A1.3: 加 zh-CN 键**

在 `frontend/src/i18n/zh-CN.ts:2943` 之前的 `checkpoint` 块末追加：

```typescript
      confirmHint: "按当前计划继续",
      modifyHint: "补充说明，系统将基于反馈重新生成",
      rejectHint: "放弃当前检查点，按原计划继续",
      modifyPlanningHint: "将基于您的反馈重跑 planner 重新生成步骤",
      rejectAbortHint: "拒绝后本轮将直接出报告，不再继续",
      hypothesisConfirmHint: "不勾选 = 验证全部假设",
```

**Step A1.4: 加 en-US 键**

在 `frontend/src/i18n/en-US.ts` 同步位置追加：

```typescript
      confirmHint: "Proceed with the current plan",
      modifyHint: "Add feedback; will rerun based on your input",
      rejectHint: "Skip this checkpoint, continue with the current plan",
      modifyPlanningHint: "Will rerun the planner with your feedback",
      rejectAbortHint: "Rejecting will publish the report immediately and end this turn",
      hypothesisConfirmHint: "No selection = verify all hypotheses",
```

**Step A1.5: 跑测试 → GREEN**

Run: `cd frontend && npx vitest run src/i18n/i18n.test.ts`
Expected: 12 个 case 全过

**Step A1.6: commit**

```bash
git add frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/i18n/i18n.test.ts
git commit -m "feat(i18n): 加 research.checkpoint 6 个 hint 键（中英）"
```

---

### Task A2: CheckpointCard 按钮加 Tooltip 与上下文化说明

**Files:**
- Modify: `frontend/src/components/research/CheckpointCard.tsx:303-357`（`CheckpointActions` 组件）
- Test: `frontend/src/tests/ResearchCheckpointCard.test.tsx`（已有；扩 case 验证 tooltip）

**Step A2.1: 写失败测试**

```typescript
// frontend/src/tests/ResearchCheckpointCard.test.tsx（追加）
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CheckpointCard } from "@/components/research/CheckpointCard";
import { buildCheckpoint } from "./fixtures/research";

describe("CheckpointCard tooltip per phase", () => {
  it("planning phase: modify button uses modifyPlanningHint", () => {
    const onAnswer = vi.fn();
    render(
      <CheckpointCard
        checkpoint={buildCheckpoint({ phase: "planning" })}
        onAnswer={onAnswer}
      />,
    );
    // antd Tooltip 包了 Button；触发 hover 后断言
    const modifyBtn = screen.getByRole("button", { name: /修改|modify/i });
    fireEvent.mouseOver(modifyBtn);
    expect(
      screen.getByText(/将基于您的反馈重跑 planner 重新生成步骤/),
    ).toBeInTheDocument();
  });

  it("intent phase: modify button uses generic modifyHint", () => {
    const onAnswer = vi.fn();
    render(
      <CheckpointCard
        checkpoint={buildCheckpoint({ phase: "intent" })}
        onAnswer={onAnswer}
      />,
    );
    const modifyBtn = screen.getByRole("button", { name: /修改|modify/i });
    fireEvent.mouseOver(modifyBtn);
    expect(screen.getByText(/补充说明/)).toBeInTheDocument();
  });

  it("runtime_dynamic phase: reject button uses rejectAbortHint", () => {
    const onAnswer = vi.fn();
    render(
      <CheckpointCard
        checkpoint={buildCheckpoint({ phase: "runtime_dynamic" })}
        onAnswer={onAnswer}
      />,
    );
    const rejectBtn = screen.getByRole("button", { name: /拒绝|reject/i });
    fireEvent.mouseOver(rejectBtn);
    expect(screen.getByText(/拒绝后本轮将直接出报告/)).toBeInTheDocument();
  });

  it("hypothesis phase: confirm button shows hypothesisConfirmHint", () => {
    const onAnswer = vi.fn();
    render(
      <CheckpointCard
        checkpoint={buildCheckpoint({ phase: "hypothesis", candidates: [] })}
        onAnswer={onAnswer}
      />,
    );
    const confirmBtn = screen.getByRole("button", { name: /确认|confirm/i });
    fireEvent.mouseOver(confirmBtn);
    expect(screen.getByText(/不勾选 = 验证全部假设/)).toBeInTheDocument();
  });
});
```

`buildCheckpoint` fixture 来自 `frontend/src/tests/fixtures/research.ts`（如不存在则需先建；按已有 test 模式 `as any` 构造亦可）。

**Step A2.2: 跑测试 → RED**

Run: `cd frontend && npx vitest run src/tests/ResearchCheckpointCard.test.tsx`
Expected: 4 个新 case 失败（tooltip 文案缺失）

**Step A2.3: 改 CheckpointCard.tsx**

把 311-356 行的 `CheckpointActions` 改写为：

```tsx
function CheckpointActions({
  onConfirm,
  onReject,
  onModify,
  phase,
}: CheckpointActionsProps & { phase: CheckpointPhase }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  // 上下文化 hint key 选择
  const modifyHintKey =
    phase === "planning"
      ? "research.checkpoint.modifyPlanningHint"
      : "research.checkpoint.modifyHint";
  const rejectHintKey =
    phase === "runtime_dynamic" || phase === "low_confidence_step"
      ? "research.checkpoint.rejectAbortHint"
      : "research.checkpoint.rejectHint";
  const confirmHintKey =
    phase === "hypothesis"
      ? "research.checkpoint.hypothesisConfirmHint"
      : "research.checkpoint.confirmHint";

  const submitModify = () => {
    const question = draft.trim();
    if (question.length === 0) return;
    onModify(question);
    setEditing(false);
    setDraft("");
  };

  return (
    <>
      <Space style={{ marginTop: 12 }}>
        <Tooltip title={t(confirmHintKey)}>
          <Button type="primary" onClick={onConfirm}>
            {t("research.checkpoint.confirm")}
          </Button>
        </Tooltip>
        <Tooltip title={t(modifyHintKey)}>
          <Button onClick={() => setEditing((value) => !value)}>
            {t("research.checkpoint.modify")}
          </Button>
        </Tooltip>
        <Tooltip title={t(rejectHintKey)}>
          <Button danger onClick={onReject}>
            {t("research.checkpoint.reject")}
          </Button>
        </Tooltip>
      </Space>
      {editing && (
        <div style={{ marginTop: 12 }}>
          <Input.TextArea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t("research.checkpoint.modifyPlaceholder")}
            autoSize={{ minRows: 2 }}
          />
          <Button
            type="primary"
            onClick={submitModify}
            style={{ marginTop: 8 }}
            disabled={draft.trim().length === 0}
          >
            {t("research.checkpoint.submitModify")}
          </Button>
        </div>
      )}
    </>
  );
}
```

把 `CheckpointCard` 调 `CheckpointActions` 的位置（line 391 附近）增加 `phase={checkpoint.phase}` 传参。

**Step A2.4: 跑测试 → GREEN**

Run: `cd frontend && npx vitest run src/tests/ResearchCheckpointCard.test.tsx`
Expected: 4 个新 case 全过；既有 case 不退化

**Step A2.5: commit**

```bash
git add frontend/src/components/research/CheckpointCard.tsx frontend/src/tests/ResearchCheckpointCard.test.tsx
git commit -m "feat(checkpoint): 三按钮加 Tooltip，按 phase 上下文化 hint"
```

---

## Phase B — 可靠性收口（深度审计发现的 Critical/Important）

### Task B1: 加 max-replan 计数器，封堵 planning modify 无限循环

**Files:**
- Modify: `backend/app/services/research_agent_ports.py`（新增 `MAX_REPLAN_PER_TURN = 3` 常量）
- Modify: `backend/app/services/research_agent_service.py:281-285`（replan 写时检查）
- Modify: `backend/app/services/research_agent_phases.py:162-182`（replan 读时检查）
- Test: `backend/app/tests/integration/test_research_agent_service.py`（追加 modify 循环测试）

**Interfaces:**
- `MAX_REPLAN_PER_TURN = 3` 常量已存 `research_agent_ports.py`；service 与 phases 仅消费

**Step B1.1: 写失败测试**

```python
# backend/app/tests/integration/test_research_agent_service.py（追加）
async def test_planning_modify_replan_caps_at_max_replan(db_session, fake_llm):
    """第 4 次 planning modify 应不再触发 replan，转成 confirm 继续执行。"""
    # 模拟用户连续 modify planning checkpoint 4 次
    # 断言：第 4 次 modify 后，plan phase 不再被重新调用（planner.call_count == 3）
    # 断言：第 4 次 modify 直接进入 execute phase，不再开 checkpoint
    ...
```

**Step B1.2: 跑测试 → RED**

Run: `cd backend && pytest -x -q backend/app/tests/integration/test_research_agent_service.py::test_planning_modify_replan_caps_at_max_replan`
Expected: FAIL（当前实现没有 cap，会跑出第 4 次 replan 或 timeout）

**Step B1.3: 加常量**

`backend/app/services/research_agent_ports.py` 在常量区块追加：

```python
# 研究计划阶段 modify 重生成上限；超过则按 confirm 处理，避免无限循环。
MAX_REPLAN_PER_TURN = 3
```

**Step B1.4: 改 write 服务**

`research_agent_service.py:281-285` 改成：

```python
        elif action == ACTION_MODIFY and checkpoint.phase == CHECKPOINT_PLANNING:
            # Task 6.5-4 + 2026-10-07 P1：replan 上限封顶。超过则不再 replan，按 confirm 处理。
            replan_count = int(state.get("replanCount", 0))
            if replan_count < MAX_REPLAN_PER_TURN:
                startPhase = PHASE_PLAN
                state["replan"] = True
                state["replanCount"] = replan_count + 1
            else:
                # 上限已到，按 confirm 处理，继续推进
                startPhase = nextPhaseForPhase(checkpoint.phase, options)
                logger.info(
                    "replan 上限已达 %s，按 confirm 推进: session=%s",
                    MAX_REPLAN_PER_TURN, checkpoint.session_id,
                )
```

**Step B1.5: 改 read 端（不变逻辑，只记录日志）**

`research_agent_phases.py:162-182` 已有 `state.pop("replan", False)` 消费逻辑；只需在 plan phase 入口处加日志（`logger.info("第 %s 次 replan", state["replanCount"])`），便于排查。

**Step B1.6: 跑测试 → GREEN**

Run: `cd backend && pytest -x -q backend/app/tests/integration/test_research_agent_service.py -k "replan or modify"`
Expected: 新 case + 既有 case 全过

**Step B1.7: commit**

```bash
git add backend/app/services/research_agent_ports.py backend/app/services/research_agent_service.py backend/app/services/research_agent_phases.py backend/app/tests/integration/test_research_agent_service.py
git commit -m "fix(research): planning modify 加 max-replan=3 上限，防无限循环"
```

---

### Task B2: ResearchCheckpoint 加 expires_at + resolve 时的 staleness 校验

**Files:**
- Modify: `backend/app/domain/research_models.py:103-126`（加 `expires_at` 字段）
- Create: `backend/alembic/versions/<timestamp>_add_research_checkpoint_expires_at.py`（迁移）
- Modify: `backend/app/services/research_session_service.py:162-201`（resolve 时 staleness 校验）
- Modify: `backend/app/services/research_agent_service.py:_pauseForUser`（开 checkpoint 时写入 expires_at）
- Modify: `backend/app/services/research_session_service.py:getPendingCheckpoint`（查 pending 时过滤掉已过期）
- Test: `backend/app/tests/integration/test_research_session_service.py`（追加过期场景）

**Interfaces:**
- `ResearchCheckpoint.expires_at: datetime | None`（nullable，向后兼容）
- TTL 默认 24h，可通过环境变量 `RESEARCH_CHECKPOINT_TTL_HOURS` 覆盖

**Step B2.1: 写失败测试**

```python
# backend/app/tests/integration/test_research_session_service.py（追加）
async def test_resolve_checkpoint_rejects_stale(db_session):
    """expires_at 已过的 checkpoint 视为已非 pending。"""
    cp = await _seedCheckpoint(db_session, status="pending", expires_at=datetime.utcnow() - timedelta(hours=1))
    with pytest.raises(ValueError, match="过期"):
        await svc.resolveCheckpoint(db_session, cp.id, "confirmed", {})
```

**Step B2.2: 跑测试 → RED**

Run: `cd backend && pytest -x -q backend/app/tests/integration/test_research_session_service.py::test_resolve_checkpoint_rejects_stale`
Expected: FAIL（当前没有 expires_at 字段，也没有 staleness 校验）

**Step B2.3: 加 model 字段**

`backend/app/domain/research_models.py` 的 `ResearchCheckpoint` 加：

```python
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
```

**Step B2.4: 写迁移**

生成：`cd backend && alembic revision --autogenerate -m "add research_checkpoint.expires_at"`

人工 review autogenerate 输出（确保只新增一列 `expires_at`，不动其他）。

**Step B2.5: 跑迁移**

Run: `cd backend && alembic upgrade head`
Expected: 迁移成功，无报错

**Step B2.6: 改 resolve 校验**

`research_session_service.py:184-198` 的原子 UPDATE 加上 expires_at 条件：

```python
    update_stmt = (
        update(ResearchCheckpoint)
        .where(
            ResearchCheckpoint.id == checkpointId,
            ResearchCheckpoint.status == CHECKPOINT_PENDING,
            or_(
                ResearchCheckpoint.expires_at.is_(None),
                ResearchCheckpoint.expires_at > datetime.utcnow(),
            ),
        )
        .values(status=status, user_choice=userChoice, decided_at=...)
    )
```

`rowcount == 0` 时分两种情况判断：
- 重新查询 status
- 若 status 仍为 pending 但 expires_at ≤ now → 抛 `StaleCheckpointError`（新增异常类，继承 ValueError 以便上层统一处理）
- 否则按原逻辑抛「非 pending」

**Step B2.7: 改 _pauseForUser 写 expires_at**

`research_agent_service.py:_pauseForUser` 在调 `openCheckpoint` 前计算 `expires_at = now + timedelta(hours=settings.RESEARCH_CHECKPOINT_TTL_HOURS)`，写入 options 或 checkpoint 行。

**Step B2.8: 改 getPendingCheckpoint 过滤**

`research_session_service.py:getPendingCheckpoint` 加 WHERE 条件：

```python
        .where(
            ResearchCheckpoint.session_id == sessionId,
            ResearchCheckpoint.status == CHECKPOINT_PENDING,
            or_(
                ResearchCheckpoint.expires_at.is_(None),
                ResearchCheckpoint.expires_at > datetime.utcnow(),
            ),
        )
```

**Step B2.9: 跑测试 → GREEN**

Run: `cd backend && pytest -x -q backend/app/tests/integration/test_research_session_service.py backend/app/tests/integration/test_research_agent_service.py`
Expected: 新 case + 既有 case 全过

**Step B2.10: commit**

```bash
git add backend/app/domain/research_models.py backend/alembic/versions/ backend/app/services/research_session_service.py backend/app/services/research_agent_service.py backend/app/tests/integration/
git commit -m "feat(research): ResearchCheckpoint 加 expires_at + staleness 校验 + 过滤"
```

---

### Task B3: 并发提交 500 → 409

**Files:**
- Modify: `backend/app/services/research_session_service.py`（新增 `CheckpointConflictError` 异常类，区分已决 vs 过期）
- Modify: `backend/app/api/v1/research.py:401-416`（捕获并转 409）

**Step B3.1: 写失败测试**

```python
# backend/app/tests/integration/test_research_api.py（追加）
async def test_concurrent_checkpoint_answer_returns_409(client, db_session):
    """同一 checkpoint 被并发提交两次，第二次返回 409。"""
    # 第一次：200 OK
    # 第二次：409 Conflict（当前实现是 500）
    ...
```

**Step B3.2: 跑测试 → RED**

Run: `cd backend && pytest -x -q backend/app/tests/integration/test_research_api.py::test_concurrent_checkpoint_answer_returns_409`
Expected: FAIL（当前第二次返回 500）

**Step B3.3: 新增异常类**

`research_session_service.py`：

```python
class CheckpointConflictError(Exception):
    """checkpoint 已被决 / 过期 / 状态非法 → 应返回 409。"""

class CheckpointStaleError(CheckpointConflictError):
    """checkpoint 已过期。"""
```

把 B2 中 `resolveCheckpoint` 抛的 `ValueError`（stale 路径）改成 `CheckpointStaleError`；非 pending 路径抛 `CheckpointConflictError`。

**Step B3.4: 改 FastAPI handler**

`api/v1/research.py:401-416` 加捕获：

```python
from app.services.research_session_service import CheckpointConflictError

@router.post("/checkpoints/{checkpointId}/answer", ...)
async def answer_checkpoint(...):
    try:
        await svc.resolveCheckpoint(...)
    except CheckpointConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
```

**Step B3.5: 跑测试 → GREEN**

Run: `cd backend && pytest -x -q backend/app/tests/integration/test_research_api.py -k "concurrent or 409"`
Expected: 新 case + 既有 409 测试全过

**Step B3.6: commit**

```bash
git add backend/app/services/research_session_service.py backend/app/api/v1/research.py backend/app/tests/integration/test_research_api.py
git commit -m "fix(research): checkpoint 并发提交改 409，区分 stale vs non-pending"
```

---

### Task B3.5: 前端 SSE `checkpoint_conflict` 处理（plan 追加）

> **追加原因**：B3 实现路径选了 SSE 事件而非同步 HTTP 409，前端需对应识别 `code=checkpoint_conflict` / `uiHint=conflict` 并 toast 给用户，否则用户感知不到（流保持打开、无错误提示）。本任务在 B3 落地后单独追加。

**Files:**
- Modify: `frontend/src/types/research.ts:110`（扩展 `ResearchErrorUiHint` union）
- Modify: `frontend/src/stores/researchStore.ts:79`（增加 `uiHint === "conflict"` 分支）
- Modify: `frontend/src/i18n/zh-CN.ts` + `en-US.ts`（加 1 个 key：`research.error.checkpoint_conflict`）
- Modify: `frontend/src/pages/research/ResearchSessionPage.tsx`（订阅 `conflictError` 字段并 toast）
- Test: `frontend/src/tests/researchStore.test.ts`（已有；扩 case 验证 conflict 分支）

**Step B3.5.1: 写失败测试**

```typescript
// frontend/src/tests/researchStore.test.ts（追加）
import { applyResearchEvent, buildInitialState } from "@/stores/researchStore";

describe("research.error conflict branch", () => {
  it("uiHint=conflict sets error state and closes stream", () => {
    const initial = buildInitialState();
    const event = {
      type: "research.error",
      payload: { code: "checkpoint_conflict", message: "...", uiHint: "conflict" },
    };
    const next = applyResearchEvent(initial, event);
    expect(next.error).toBeTruthy();
    expect(next.streaming).toBe(false);
  });

  it("uiHint=degraded still falls through (existing behavior)", () => {
    const initial = buildInitialState();
    const event = {
      type: "research.error",
      payload: { code: "step_failed", uiHint: "degraded" },
    };
    const next = applyResearchEvent(initial, event);
    expect(next.error).toBeNull();
    expect(next.streaming).toBe(true);
  });
});
```

**Step B3.5.2: 跑测试 → RED**

Run: `cd frontend && npx vitest run src/tests/researchStore.test.ts -k "conflict"`
Expected: 失败 — TS 编译期 `uiHint: "conflict"` 非法（union 未扩展）；运行时新分支未实现。

**Step B3.5.3: 扩展 ResearchErrorUiHint union**

`frontend/src/types/research.ts:110`：

```typescript
export type ResearchErrorUiHint = "terminal" | "degraded" | "conflict";
```

**Step B3.5.4: store 加 conflict 分支**

`frontend/src/stores/researchStore.ts:79`（在 `research.error` case 内 `isTerminalError` 判断之前）：

```typescript
case "research.error": {
  if (isTerminalError(event.payload)) {
    return { events, streaming: false,
      error: typeof event.payload.message === "string" ? event.payload.message : "research.error" };
  }
  if (event.payload.uiHint === "conflict") {
    // B3.2 落地后引入：并发 checkpoint 决策冲突。流关闭 + 暴露 conflictError 供页面 toast。
    const message =
      typeof event.payload.message === "string" && event.payload.message.length > 0
        ? event.payload.message
        : "research.error.checkpoint_conflict";
    return {
      events,
      streaming: false,
      error: message,
      conflictError: message,
    };
  }
  // 降级类（degraded / 字段缺失）：流保持打开，状态机会继续推进。
  return { events };
}
```

state 加 `conflictError: string | null = null`。

**Step B3.5.5: 加 i18n 键**

`frontend/src/i18n/zh-CN.ts` `research` 命名空间下追加：

```typescript
error: {
  checkpoint_conflict: "并发检查点冲突：另一请求已先一步决/过期，请刷新后重试",
},
```

`frontend/src/i18n/en-US.ts` 同步：

```typescript
error: {
  checkpoint_conflict: "Checkpoint conflict: another request already decided or expired; please refresh and retry",
},
```

**Step B3.5.6: 页面 toast**

`frontend/src/pages/research/ResearchSessionPage.tsx`：在合适位置（useEffect 或订阅 selector）订阅 `useResearchStore((s) => s.conflictError)`，当从非空切到非空时调 `message.error(t(value))`，然后 dispatch 一个 reset action 把 `conflictError` 清回 null（避免重复 toast）。

**Step B3.5.7: 跑测试 → GREEN**

Run: `cd frontend && npx vitest run src/tests/researchStore.test.ts -k "conflict"`
Expected: 新 case 全过；既有 case 不退化。

**Step B3.5.8: commit**

```bash
git add frontend/src/types/research.ts frontend/src/stores/researchStore.ts frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/pages/research/ResearchSessionPage.tsx frontend/src/tests/researchStore.test.ts
git commit -m "feat(checkpoint): 前端识别 SSE checkpoint_conflict 事件并提示用户"
```

---

### Task B4: SSE 重连时断 — answer 提交后保留「pending resolution」中间态

**Files:**
- Modify: `frontend/src/stores/researchStore.ts:231-241`（answer 提交时不立即清空 pendingCheckpoint，加 resolution 标志）
- Modify: `frontend/src/components/research/CheckpointCard.tsx`（显示「处理中…」态）
- Test: `frontend/src/tests/researchStore.test.ts`（已有；扩 case）

**Step B4.1: 写失败测试**

```typescript
// frontend/src/tests/researchStore.test.ts（追加）
it("answer submit: pendingCheckpoint clears only after next checkpoint event arrives", async () => {
  // mock apiAnswerCheckpoint 200
  // 断言：API 调用后 pendingCheckpoint 仍然存在（带 resolutionInFlight=true）
  // 模拟下一个 SSE research.checkpoint 事件到来 → 断言 pendingCheckpoint 切换到新值
});
```

**Step B4.2: 跑测试 → RED**

Run: `cd frontend && npx vitest run src/tests/researchStore.test.ts`
Expected: FAIL（当前实现 API 成功即清空）

**Step B4.3: 改 store**

`researchStore.ts`：

```typescript
interface ResearchState {
  ...
  pendingCheckpoint: CheckpointEvent | null;
  resolutionInFlight: boolean;  // 新增：true 表示已提交答案、等待下一个 SSE
  ...
}

answer: async (checkpointId, action, choice) => {
  set({ resolutionInFlight: true });  // 不立刻清空
  try {
    await apiAnswerCheckpoint(checkpointId, { action, choice: choice ?? {} });
    // 成功 → 等 SSE 自然替换；不主动清空
  } catch (err) {
    set({ resolutionInFlight: false });  // 失败 → 取消中间态，保留 pending 让用户重试
    throw err;
  }
},

applyEvent: (event) => {
  ...
  if (event.type === "research.checkpoint") {
    set({ pendingCheckpoint: checkpointFromEvent(event.payload), resolutionInFlight: false });
  }
  ...
}
```

**Step B4.4: CheckpointCard 渲染中间态**

```tsx
{resolutionInFlight && (
  <Spin size="small" style={{ marginLeft: 12 }}>
    {t("research.checkpoint.processing")}
  </Spin>
)}
```

i18n 加 `checkpoint.processing: "处理中…"`（zh + en）。

**Step B4.5: 跑测试 → GREEN**

Run: `cd frontend && npx vitest run src/tests/researchStore.test.ts src/tests/ResearchCheckpointCard.test.tsx`
Expected: 全过

**Step B4.6: commit**

```bash
git add frontend/src/stores/researchStore.ts frontend/src/components/research/CheckpointCard.tsx frontend/src/i18n/ frontend/src/tests/
git commit -m "fix(checkpoint): answer 提交后保留中间态，避免 SSE 重连时短暂清空"
```

---

### Task B5: 答案落 audit_log（满足长期审计需求）

**Files:**
- Modify: `backend/app/services/research_session_service.py:resolveCheckpoint`（写完 checkpoint 后同步落 audit_log）
- Test: `backend/app/tests/integration/test_research_session_service.py`（追加 audit_log 写入测试）

**Step B5.1: 写失败测试**

```python
async def test_resolve_checkpoint_writes_audit_log(db_session):
    """checkpoint answer 后 audit_log 应有对应记录。"""
    cp_id = await _seedCheckpoint(db_session, status="pending")
    await svc.resolveCheckpoint(db_session, cp_id, "confirmed", {"selectedIndexes": [0, 1]})
    row = (await db_session.execute(
        select(AuditLog).where(AuditLog.entity_type == "research_checkpoint")
    )).scalar_one()
    assert row.action == "resolved"
    assert row.payload["status"] == "confirmed"
```

**Step B5.2: 跑测试 → RED**

Run: `cd backend && pytest -x -q backend/app/tests/integration/test_research_session_service.py::test_resolve_checkpoint_writes_audit_log`
Expected: FAIL（当前未写 audit_log）

**Step B5.3: resolveCheckpoint 内追加 audit_log 写入**

`research_session_service.py:resolveCheckpoint` 在事务内 UPDATE 完成后追加：

```python
    db_session.add(AuditLog(
        entity_type="research_checkpoint",
        entity_id=str(checkpointId),
        action="resolved",
        actor_user_id=...,  # 从上层 session 取
        payload={
            "status": status,
            "user_choice": userChoice,
            "session_id": checkpoint.session_id,
        },
        created_at=datetime.utcnow(),
    ))
    await db_session.flush()
```

`actor_user_id` 需要 resolveCheckpoint 的签名增加 `user_id: Decimal | None` 参数；上层 `research_agent_service.py:resumeTurn` 调用处透传 `state.get("userId")` 或 `row.user_id`。

**Step B5.4: 跑测试 → GREEN**

Run: `cd backend && pytest -x -q backend/app/tests/integration/test_research_session_service.py backend/app/tests/integration/test_research_api.py`
Expected: 新 case + 既有 case 全过

**Step B5.5: commit**

```bash
git add backend/app/services/research_session_service.py backend/app/services/research_agent_service.py backend/app/tests/integration/
git commit -m "feat(research): checkpoint 决议落 audit_log，便于长期审计"
```

---

## Phase C — UX 细节抛光（Minor，可延后）

> 这部分为 minor polish，可在 Phase A+B 完成后视情况追加。本次计划默认延后。

### Task C1（可选）: Loading spinner + reject 二次确认 + 按钮提交期间禁用

包含 4 个 sub-改动：
- 加 loading 态 disable 三按钮（双击保护）
- reject 按钮加 Popconfirm 二次确认（runtime_dynamic / low_confidence_step 时强制）
- modify 提交按钮 disabled=isSubmitting
- 全程 loading spinner

文件：`frontend/src/components/research/CheckpointCard.tsx`

---

## 完成检查

### 每 Phase 后

```bash
cd backend && pytest -x -q backend/app/tests/unit/ backend/app/tests/integration/test_research_*.py 2>&1 | tail -20
cd frontend && npx vitest run 2>&1 | tail -20
```

期望：全绿。

### 全部 Phase 后

```bash
# A1+A2 验证：i18n 键 + Tooltip
cd frontend && npx vitest run src/i18n/i18n.test.ts src/tests/ResearchCheckpointCard.test.tsx

# B1 验证：replan 上限生效（连续 4 次 modify 不再触发第 4 次 planner）
cd backend && pytest -x -q backend/app/tests/integration/test_research_agent_service.py -k "replan"

# B2 验证：stale checkpoint 拒绝 resolve
cd backend && pytest -x -q backend/app/tests/integration/test_research_session_service.py -k "stale or expires"

# B3 验证：并发提交第二次 = 409
cd backend && pytest -x -q backend/app/tests/integration/test_research_api.py -k "concurrent or 409"

# B5 验证：audit_log 写入
cd backend && pytest -x -q backend/app/tests/integration/test_research_session_service.py -k "audit_log"
```

### 自检清单

- [ ] Phase A：A1+A2 落地，i18n 12 case + tooltip 4 case 全绿
- [ ] Phase B：B1-B5 全部 case + 既有研究 case 全绿
- [ ] grep 验证：`routing_layer=`, `purpose="x"`, `action=` 在业务代码仍走 SSOT
- [ ] alembic 迁移文件已生成，B2 schema 改动有对应迁移
- [ ] commit message 全部 `refactor/feat/fix` conventional 格式
- [ ] 没有原地 mutation
- [ ] 没有改既有测试断言（除扩 case）
- [ ] 没有改 `messages_zh.py`（与上一计划隔离）

### 风险与回滚

- **风险 1：B2 迁移失败导致 schema 漂移** → 缓解：先在测试库 upgrade head，再上线 → 回滚：`alembic downgrade -1`
- **风险 2：B4 中间态卡住导致 pendingCheckpoint 永不消失** → 缓解：SSE 断线时 frontend 应主动重置 `resolutionInFlight=false`（可在 `connectStream` 失败 handler 中加）→ 已在 plan 中标注
- **风险 3：B5 audit_log 写入失败导致整个 resolve 失败** → 缓解：把 audit_log 写入放在事务里；写失败即整体回滚，与 DB 一致性优先

---

## 不在本期范围（后续 backlog）

- Task C1: Loading spinner / reject 二次确认 / 按钮 disabled
- A4-A7 (Minor UX): 按钮 disabled、可读性优化
- Finding 7: hypothesis confirm 文案区分「验证全部」vs「仅验证所选」（已加 hint 键，但可再升级为单选/双 radio UI）
- A8 audit_log：补 user 维度统计报表
- TTL 自动清理 cron（手动 SSE 触发 / 启动时扫一次可缓解，但生产应配独立 cron）
- en-US locale 整体对齐（其它块的 hint 缺失）

---

**计划完成。**