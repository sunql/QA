# N4 — Qwen 多步任务中断后跨模型手动续跑 设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Chat + Research 多步任务执行过程中，Qwen 等模型因能力边界或网络问题中断后，用户可以**手动**选择另一个模型，从失败步骤开始续跑，不丢失前序步骤的上下文。

**Architecture:** 不引入后端自动路由/重试/降级。用户在 UI 显式选模型，前端把 `modelId` 透传到既有 API（Chat 走 `model_override`，Research 走新增 `model_id`）。后端在续跑时用新模型，run 状态与前序步骤结果**不**被重置。

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy / Pydantic v2 / antd 5.x / vitest / pytest

---

## 背景

### 用户场景

仓库内最高难度复合 prompt 之一（B019 圣特公司 12 月供货量下降分析），Chat 4 步多步任务执行到第 3 步时，Qwen3.8-27B-4bit 因对比推理能力不够返回「无法回答」软失败。当前唯一恢复手段是「重新发送」整条问题，丢掉 1-2 步已出数据 + 重新烧一份 LLM 费用。

用户希望：失败后弹一个模型下拉，选 deepseek-chat 从 step 3 续跑；前 2 步结果作为上下文喂给 deepseek，1 步的钱 + 2 步的时间保留。

### 复用现有设施

- ✅ **Chat 后端**：`ResumeRequest.model_override` 字段已存在（`backend/app/api/v1/chat.py:143`），`POST /chat/multi-step/{runId}/resume` 已透传 `dto.model_override or run.model_id`（`chat.py:219`）
- ✅ **Chat 续跑核心**：`backend/app/services/multi_step_resume.py` 106 行完整实现，包含行锁、幂等键、状态闸、压缩清理
- ✅ **Research 后端**：`POST /research/checkpoints/{checkpointId}/answer` 已在（`backend/app/api/v1/research.py:556`），但 `CheckpointAnswerRequest` 无 `model_id` 字段
- ✅ **Research 续跑核心**：`service.resumeTurn(checkpointId, action, choice)` 已在（`backend/app/services/research_agent_service.py`）
- ✅ **模型下拉源**：`listLlmModels()` 已在（per memory `qa-system-llm-model-selector`）

### 缺口（gaps）

1. **Chat 前端 API 函数丢失**：`frontend/src/api/chat.ts:0` 中 `resumeMultiStepRun` 函数**不存在**（2 个测试 `chatApi.test.ts:728, 741` 失败 `TypeError: resumeMultiStepRun is not a function`）。这是 P2 followups 期间（`b958b855` 合并 chatStore 改动时）误删的 — 没有保留 API 端点导致 SSE 续跑被 `StreamChunkParser` 接管，函数被删但测试 / store 仍引用。
2. **Chat 前端 UI 缺模型选择**：`ResumeRunButton.tsx` 只 `onResume(runId, fromStepIndex)`，无 `modelId` 参数
3. **Research 后端 schema 缺字段**：`CheckpointAnswerRequest` 无 `model_id`
4. **Research UI 缺模型选择**：checkpoint 决策弹窗未带模型下拉
5. **缺失的跨链路契约**：研究 turn 当前 turn.model_id 是固定的，续跑时如何让后续 llm_calls 用新模型 — `service.resumeTurn` 当前不接 model 参数

### Why 此 SSOT 单独存在

承接 P2 SSOT N4 「qwen-uplift 知识保留 / 按任务匹配模型」未达成项 + 用户 2026-10-17 反馈的「Qwen 中断后能手动续跑」真实痛点。原 N4 路线（按内容自动改派模型）已被 `qa-system-no-model-override` 决策否决，本设计采用「用户显式选模型」架构。

---

## Global Constraints

- **禁止后端覆盖用户 modelId**（per memory `qa-system-no-model-override.md`）— 本设计**不**引入任何路由 hook、自动 fallback、内容改派
- **不增表、不增 alembic 迁移**（per 之前的 revert commit `c2f12b4e` 教训）— 仅用现有 `MultiStepRun.model_id` 列 / `ResearchTurn.model_id` 列
- **真实数据库测试**（per memory `qa-system-integration-env-contract`）— 后端测试用真 PG + 完整 API 链路
- **不可变数据**（per `qa-system-coding-standards` #1）— frozen dataclass 必须用 `dataclasses.replace`
- **小文件**（per P2 SSOT 编码约定）— 新增函数 ≤ 50 行、新增文件 ≤ 400 行
- **80%+ 覆盖率**（per P2 SSOT 核心约束 #4）— 已有覆盖率基线不倒退
- **TypeScript strict mode**（per frontend 项目）— 不引入 `any`，公共 API 显式类型
- **Pydantic v2 + extra=forbid**（per memory `qa-system-camelmodel-extra-forbid`）— 新 schema 字段必须 reject unknown

---

## Architecture & Data Flow

### 端到端流程

```
Chat 路径:
  user 点续跑 → 弹 Popover (Select 原模型或他模型) → 选 deepseek
    → POST /chat/multi-step/{runId}/resume {from_step_index, model_override: deepseek.id}
    → prepareResume() 状态闸 (与原逻辑同) → adoptRunForResume 用新 modelId
    → 后续 _streamQuery / _tryFeatureResponse / multi-step LLM call 全部走新 model

Research 路径:
  Qwen 中断 → 后台创建 checkpoint (action=continue) → 用户看 checkpoint
    → 弹 CheckpointDecisionModal (现有 action 选择 + 新 model dropdown)
    → POST /research/checkpoints/{id}/answer {action, choice, model_id: deepseek.id}
    → _resumeTurnInBackground 调 service.resumeTurn(..., modelId=deepseek.id)
    → 写 turn.model_id = deepseek.id (audit_log 记 change)
    → 后续 turn 的 llm_calls 走 deepseek
```

**双路独立、后端不自动决策**。

### 状态守恒

- 续跑**不**重置前序步骤结果（`STEP_STATUS_SUCCEEDED` / `STEP_STATUS_COMPRESSED` 保留）
- 续跑**只**重置从 `fromStepIndex` 开始的步骤（`prepareResume` 已有逻辑，per `multi_step_resume.py:79-83`）
- 续跑**不**改 `MultiStepRun.question` / `datasourceId`（per `multi_step_resume.py:101-105`）
- 续跑**改** `MultiStepRun.model_id`（新行为）

---

## API Surface Changes

### Chat 后端（仅使用，不改）

```python
# 现有: backend/app/api/v1/chat.py:141-144
class ResumeRequest(CamelModel):
    from_step_index: int | None = None
    model_override: int | None = None  # ← 使用
    compress_again: bool = False
```

```python
# 现有: backend/app/api/v1/chat.py:215-221
chatDto = ChatRequest(
    question=run.question,
    sessionId=str(run.session_id),
    datasourceId=run.datasource_id,
    modelId=dto.model_override or run.model_id,  # ← 已有透传
    resumeRunId=run.id,
)
```

### Research 后端（新增 schema + 服务参数）

```python
# backend/app/domain/research_schemas.py:88-103
class CheckpointAnswerRequest(CamelModel):
    """POST /research/checkpoints/{checkpointId}/answer 请求体。"""

    model_config = ConfigDict(extra="forbid")  # 保持

    action: CheckpointAction
    choice: dict[str, Any] = Field(default_factory=dict)
    # ── 新增 ──
    model_id: int | None = Field(
        default=None,
        gt=0,
        alias="modelId",
        description="续跑模型 id（None = 沿用原 turn.model_id）",
    )
```

```python
# backend/app/services/research_agent_service.py
async def resumeTurn(
    self,
    session: AsyncSession,
    *,
    checkpointId: uuid.UUID,
    action: str,
    choice: dict[str, Any],
    model_id: int | None = None,  # ← 新增
    emit: Emit | None = None,
) -> str:
    ...
    if model_id is not None:
        turn = await session.get(ResearchTurn, ...)
        old_model_id = turn.model_id
        turn.model_id = model_id
        # audit_log: actor + action=resume_model_change + before/after
        await _audit_resume_model_change(...)
```

```python
# backend/app/api/v1/research.py:556-609
async def answerCheckpoint(
    ...
    payload: CheckpointAnswerRequest,  # 已含 model_id
    ...
) -> CheckpointAnswerRead:
    ...
    task = await enqueueTask(
        db,
        sessionId=checkpoint.session_id,
        turnId=checkpoint.turn_id,
        kind=TASK_KIND_RESUME,
        payload={
            "checkpointId": str(checkpoint.id),
            "action": payload.action,
            "choice": choice,
            "modelId": payload.model_id,  # ← 新增
        },
    )
```

```python
# backend/app/api/v1/research.py:927+
async def _resumeTurnInBackground(
    taskId, checkpointId, checkpointSessionId, action, choice,
    modelId: int | None = None,  # ← 新增
):
    await service.resumeTurn(
        session, checkpointId=checkpointId, action=action, choice=choice,
        model_id=modelId,  # ← 新增
    )
```

### 前端 API + UI

```typescript
// frontend/src/api/chat.ts （新增）
export async function resumeMultiStepRun(
  runId: string,
  fromStepIndex: number,
  handlers: StreamEventHandlers,
  options?: { modelId?: number | null },  // ← 新增
): Promise<void> {
  const headers = authHeaders({ "Content-Type": "application/json" });
  if (options?.modelId) {
    headers["Idempotency-Key"] = `resume-${runId}-${Date.now()}`;
  }
  const response = await fetch(
    `${API_BASE_URL}${BASE}/multi-step/${encodeURIComponent(runId)}/resume`,
    {
      method: "POST",
      headers,
      body: JSON.stringify({
        from_step_index: fromStepIndex,
        model_override: options?.modelId ?? null,  // null = 用 run 原 model
      }),
    }
  );
  if (!response.ok) throw new Error(`续跑失败 (HTTP ${response.status})`);
  // ... SSE 处理同 sendMessageStream
}
```

```typescript
// frontend/src/stores/chatStore.ts:863
resumeRun: async (runId, fromStepIndex, messageId, modelId?: number | null) => {
  // 清横幅
  set(...);
  try {
    await resumeMultiStepRun(runId, fromStepIndex, streamHandlers(set, messageId), {
      modelId: modelId ?? null,
    });
  } catch (e) {
    ...
  }
}
```

```tsx
// frontend/src/components/chat/ResumeRunButton.tsx
export default function ResumeRunButton({
  runId,
  fromStepIndex,
  originalModelId,        // ← 新增：原模型 id
  onResume,               // 签名改为 (runId, fromStepIndex, modelId | null) => void
  disabled,
}: ResumeRunButtonProps) {
  const [open, setOpen] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<number | null>(null);
  const { data: models = [] } = useLlmModels();  // 复用 LlmPanel 数据源

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      content={
        <Space direction="vertical">
          <Typography.Text>选择续跑模型</Typography.Text>
          <Select
            value={selectedModelId ?? originalModelId}
            onChange={setSelectedModelId}
            options={models.map(m => ({ value: m.id, label: m.displayName }))}
            style={{ width: 200 }}
          />
          {selectedModelId === originalModelId && (
            <Typography.Text type="secondary">
              （该模型刚失败，可选重试或换其他模型）
            </Typography.Text>
          )}
          <Space>
            <Button
              type="primary"
              onClick={() => {
                onResume(runId, fromStepIndex, selectedModelId);
                setOpen(false);
              }}
            >
              用所选模型续跑
            </Button>
            <Button onClick={() => { onResume(runId, fromStepIndex, null); setOpen(false); }}>
              原模型续跑
            </Button>
            <Button onClick={() => setOpen(false)}>取消</Button>
          </Space>
        </Space>
      }
    >
      <Button type="primary" disabled={disabled} data-testid="resume-run">
        续跑
      </Button>
    </Popover>
  );
}
```

```tsx
// frontend/src/components/research/CheckpointDecisionModal.tsx （扩）
export default function CheckpointDecisionModal({ checkpoint, onSubmit, ... }) {
  const [modelId, setModelId] = useState<number | null>(checkpoint.turnModelId ?? null);
  const { data: models = [] } = useLlmModels();

  return (
    <Modal ...>
      ... 现有 action 选择 UI ...
      <Form.Item label="本轮 LLM 模型" help="原模型 Qwen 中断后可换 deepseek 重试">
        <Select
          value={modelId ?? undefined}
          onChange={setModelId}
          options={models.map(m => ({ value: m.id, label: m.displayName }))}
        />
      </Form.Item>
    </Modal>
  );
}

// onSubmit 时:
await answerCheckpoint(checkpointId, {
  action,
  choice,
  modelId: modelId ?? undefined,  // undefined = 不传
});
```

---

## Error Handling

| 场景 | 触发 | 用户看到 |
|------|------|---------|
| 模型不存在 | 404 from `ModelConfigService.get` | message.error「模型不存在，请重新选择」 |
| 模型 API key 未配置 | 503 from `createClient` | message.error「该模型 API key 未配置，请选其他」+ 「重试」按钮 |
| 原模型刚失败 | `selectedModelId === originalModelId` 且 run.status === failed | 灰色 text「该模型刚失败，可选重试或换其他模型」（仅提示，不阻） |
| 续跑 race 失败 | 409 from `ResumeConflict` | 既有横幅（per `chatStore.resumeRun` 已处理） |
| Research turn 已 finished | 409 from `decideResume` | 既有 error 文案 |

---

## Testing Strategy

### 后端 unit（2 个新增）

```python
# backend/app/tests/integration/test_chat_resume_model_override.py
async def test_resume_with_model_override_passes_through(dbSession):
    """model_override=2 → chatDto.modelId=2，run.model_id 仍为 1。"""
    run = await _create_failed_run(model_id=1, failed_step=2)
    response = await client.post(
        f"/chat/multi-step/{run.id}/resume",
        json={"from_step_index": 2, "model_override": 2},
    )
    assert response.status_code == 200
    refreshed = await loadRun(dbSession, run.id)
    assert refreshed.model_id == 1  # run 自身不更新, dto 临时覆盖

async def test_resume_with_invalid_model_returns_404(dbSession):
    """model_override=99999（不存在）→ 404。"""
    run = await _create_failed_run(model_id=1, failed_step=2)
    response = await client.post(
        f"/chat/multi-step/{run.id}/resume",
        json={"from_step_index": 2, "model_override": 99999},
    )
    assert response.status_code == 404
```

```python
# backend/app/tests/integration/test_research_checkpoint_model_id.py
async def test_checkpoint_with_model_id_updates_turn(dbSession):
    """CheckpointAnswerRequest.model_id=2 → turn.model_id=2, audit_log 记 before/after。"""
    turn = await _create_turn(model_id=1)
    checkpoint = await _create_pending_checkpoint(turn.id)
    response = await client.post(
        f"/research/checkpoints/{checkpoint.id}/answer",
        json={"action": "continue", "choice": {}, "modelId": 2},
    )
    assert response.status_code == 200
    refreshed = await dbSession.get(ResearchTurn, turn.id)
    assert refreshed.model_id == 2
    audit = await _latest_audit_log(checkpoint.session_id, action="resume_model_change")
    assert audit.before == {"model_id": 1}
    assert audit.after == {"model_id": 2}

async def test_checkpoint_without_model_id_keeps_turn_model(dbSession):
    """不传 modelId → turn.model_id 保持 1。"""
    turn = await _create_turn(model_id=1)
    checkpoint = await _create_pending_checkpoint(turn.id)
    response = await client.post(
        f"/research/checkpoints/{checkpoint.id}/answer",
        json={"action": "continue", "choice": {}},
    )
    assert response.status_code == 200
    refreshed = await dbSession.get(ResearchTurn, turn.id)
    assert refreshed.model_id == 1  # 不变
```

### 前端 unit（4 个新增 + 2 个修复）

```typescript
// frontend/src/tests/chatApi.test.ts:728, 741 （修复：resumeMultiStepRun 之前不存在）
it("resumeMultiStepRun POST 到 resume 端点，带 Idempotency-Key 与 camelCase body", async () => {
  await resumeMultiStepRun("r-1", 2, {});
  const [url, init] = vi.mocked(fetch).mock.calls[0];
  expect(String(url)).toContain("/chat/multi-step/r-1/resume");
  expect(init.method).toBe("POST");
  const body = JSON.parse(init.body);
  expect(body).toEqual({ from_step_index: 2, model_override: null });
  expect(init.headers["Idempotency-Key"]).toBeTruthy();
});

it("resumeMultiStepRun 传 modelId → body.model_override 是 modelId", async () => {
  await resumeMultiStepRun("r-1", 2, {}, { modelId: 5 });
  const [, init] = vi.mocked(fetch).mock.calls[0];
  const body = JSON.parse(init.body);
  expect(body.model_override).toBe(5);
});

it("resumeMultiStepRun 对 runId 做 URL 编码（防路径注入）", async () => {
  await resumeMultiStepRun("a/b c", 0, {});
  const [url] = vi.mocked(fetch).mock.calls[0];
  expect(String(url)).toContain("/chat/multi-step/a%2Fb%20c");
});
```

```typescript
// frontend/src/tests/ResumeRunButton.test.tsx （新增）
describe("ResumeRunButton", () => {
  it("点击续跑按钮 → 弹 Popover 含 model dropdown", ...)
  it("选 non-original model → 调 onResume(runId, fromStep, selectedModelId)", ...)
  it("点 '原模型续跑' 按钮 → 调 onResume(runId, fromStep, null)", ...)
  it("selectedModelId === originalModelId → 显示 '该模型刚失败' 提示", ...)
});
```

```typescript
// frontend/src/tests/CheckpointDecisionModal.test.tsx （扩）
describe("CheckpointDecisionModal model 选择", () => {
  it("submit 时 modelId 是 null → payload 不含 modelId 字段", ...)
  it("submit 时选了 deepseek → payload.modelId = 2", ...)
  it("help text 显示 '原模型 Qwen 中断后可换 deepseek 重试'", ...)
});
```

### E2E（1 个新增，Playwright）

```typescript
// frontend/e2e/qwen-resume.spec.ts
test("Qwen 中断 → 选 deepseek → step 3 成功", async ({ page }) => {
  // 1. mock Qwen 失败 + deepseek 成功
  // 2. 打开 chat 页面
  // 3. 触发 B019 4 步题（mock 让 Qwen step 3 返回 "无法回答"）
  // 4. 等 step 3 显示 "续跑" 按钮
  // 5. 点续跑 → 弹 Popover → 选 deepseek
  // 6. 点 "用所选模型续跑"
  // 7. 验证 step 3 用 deepseek 重跑成功
});
```

### 覆盖率

- 后端 `multi_step_resume.py` + `research_agent_service.py` 新代码 ≥ 80%
- 前端 `ResumeRunButton.tsx` + `CheckpointDecisionModal.tsx` 新代码 ≥ 80% functions
- 既有覆盖率**不倒退**

---

## File Structure

### 后端（新增 1 文件 + 改 3 文件）

| 文件 | 改动 | 行数预期 |
|------|------|---------|
| `backend/app/api/v1/research.py` | `answerCheckpoint` payload 加 `model_id` 透传；`_resumeTurnInBackground` 接 `modelId` 参数 | +10 |
| `backend/app/domain/research_schemas.py` | `CheckpointAnswerRequest` 加 `model_id` 字段 | +8 |
| `backend/app/services/research_agent_service.py` | `resumeTurn` 接 `model_id` 参数 + audit 写 | +20 |
| `backend/app/tests/integration/test_research_checkpoint_model_id.py` | 新增测试文件 | +60 |

### 前端（新增 3 文件 + 改 3 文件）

| 文件 | 改动 | 行数预期 |
|------|------|---------|
| `frontend/src/api/chat.ts` | 新增 `resumeMultiStepRun` 函数（之前误删） | +35 |
| `frontend/src/stores/chatStore.ts` | `resumeRun` 接 `modelId` 参数 + 透传 | +3 |
| `frontend/src/components/chat/ResumeRunButton.tsx` | 拆 Popover + model dropdown | +60 |
| `frontend/src/components/research/CheckpointDecisionModal.tsx` | 加 model dropdown | +25 |
| `frontend/src/tests/ResumeRunButton.test.tsx` | 新增测试 | +80 |
| `frontend/src/tests/CheckpointDecisionModal.test.tsx` | 扩 model 测试 | +30 |
| `frontend/e2e/qwen-resume.spec.ts` | 新增 E2E | +60 |

### 新增 helpers / shared

- `frontend/src/hooks/useLlmModels.ts` （如无）— 复用 LlmPanel 已有数据源
- 不新增 DB 表 / alembic migration

---

## Out of Scope（明确不做）

- ❌ 后端自动路由 / 自动 fallback / 自动重试（per `qa-system-no-model-override`）
- ❌ 新增 model 能力元数据（不去 per-model 评估"擅长什么"）
- ❌ 续跑时同时改 question（保持"原问题"的语义）
- ❌ 续跑时改 datasourceId（保持原数据源）
- ❌ 续跑时把已压缩的 step 解压重跑（per `multi_step_resume.py:85-89` 既有逻辑）
- ❌ Chat 单步 / L4 agent loop 续跑（本 plan 只 cover multi-step 续跑）
- ❌ 后端"该模型刚失败"提示（只在 UI 提示，不在后端做）

---

## Why this design

- **用户控制权**：模型选择是用户的明确意图（per `qa-system-no-model-override`）。后端不擅自改派，用户主动选择。
- **复用现有 API**：Chat 后端 0 改动（已支持 `model_override`），仅修复前端 rot + 加 Research 后端 1 字段
- **不增表、不迁移**：用既有 `MultiStepRun.model_id` / `ResearchTurn.model_id` 列，加 audit_log 现有结构
- **失败恢复最小化**：仅重跑 fromStepIndex 起（不重跑前序成功步骤）
- **测试覆盖到 E2E**：保证真实场景「Qwen 中断 → 选 deepseek → 成功」可演示

## How to apply

- 后续任何"模型选择" UI 复用 `useLlmModels()` hook + `Select` + 同样的 help text
- 后续任何"续跑"功能遵循「用户显式选模型 + 后端透传」架构，不引入后端自动决策
- 续跑 = 改 `dto.modelId` / `turn.model_id` + 不重置前序步骤，**不**改 `run.question` / `datasourceId`

---

## 链接

- 依赖：
  - `backend/app/services/multi_step_resume.py`（Chat 续跑核心）
  - `backend/app/services/research_agent_service.py`（Research 续跑核心）
  - `frontend/src/components/chat/ResumeRunButton.tsx`（Chat UI 入口）
  - `frontend/src/components/research/CheckpointDecisionModal.tsx`（Research UI 入口）
- 相关记忆：
  - [[qa-system-no-model-override]]（禁止后端改派）
  - [[qa-system-qwen-multistep-uplift]]（原 plan 教训）
  - [[qa-system-llm-model-selector]]（listLlmModels 数据源）
  - [[qa-system-research-checkpoint-recovery]]（Research checkpoint 模式）
  - [[qa-system-multistep-current-step-idx-sentinel]]（current_step_idx 哨兵）
- 关联变更：
  - [[../aichat-priority-remaining-2026-10-16]]（N4 首次出现于 P2 SSOT）
  - [[../aichat-p2-followups-2026-10-16]]（P2 SSOT 主文件）
- 上轮：[[qa-system-frontend-13-red-done]]（PR #36 同周内，frontend 13 红恢复后下一步）

## Spec self-review

- ✅ 无 placeholder / TBD / TODO
- ✅ 内部一致：API + UI + 测试都覆盖 modelId 字段
- ✅ 范围聚焦：单 plan 覆盖 1 个特性（N4 中断续跑），不扩散到其他
- ✅ 无歧义：`modelId | null` / `modelId | undefined` 语义明确
- ✅ 真实数据库测试：后端测试用真 PG（per memory `qa-system-integration-env-contract`）
- ✅ 80%+ 覆盖率：后端 + 前端 + E2E 三层覆盖

## 手动验证 checklist（替代 E2E 自动化 — 待 Playwright mockApi 加多步支持后回填）

> 触发条件: Qwen 模型在 multi-step 中途失败 (network error / 5xx / token 超限) 用户主动选 deepseek 续跑.

### 路径 A: Chat multi-step 续跑

1. 启动后端 (qa-pg-a1 5434 跑 alembic head 0130) + 前端 (vite dev)
2. chat 页选 Qwen 模型 (下拉), 输入 "B019 销量趋势", 发送
3. 等 step 1, 2 通过, step 3 (Qwen) 失败 — 步骤卡片显示 error 状态 + "续跑" 按钮
4. 点 "续跑" 按钮 → 弹 Popover, 显示 "原模型 Qwen3.8 (OLLAMA)" + dropdown 选 deepseek
5. 选 deepseek → 提交
6. 验证: step 3 重新执行, 显示 deepseek 结果
7. 后端: `audit_log` 应有 1 条 action=UPDATE, resource_type=research_turn (chat 的多步对应类似, 路径略不同)
8. 前端: dropdown label 用 ASCII 括号 "deepseek-chat (OPENAI_COMPATIBLE_PROXY)"

### 路径 B: Research checkpoint 续跑

1. 起动后端 + 前端 + Neo4j (7688)
2. 起动研究问题, 等 checkpoint 弹窗 (action=confirm/modify/reject 三选一)
3. 弹窗底部 model dropdown, 默认 "Qwen3.8 (OLLAMA)" (turn 原模型)
4. 不改 model 直接 confirm → 后端 payload 不含 modelId → session.model_id 不变
5. 重起研究, 同样弹窗, dropdown 改 deepseek → confirm → 后端 payload.modelId=1 → session.model_id=1
6. 后端: `audit_log` 应有 1 条 action=UPDATE, before.model_id=2, after.model_id=1, resource_type=research_turn, actor_id=current_user
7. 前端: dropdown label 用 ASCII 括号

### 预期 vs 实际对照

| 步骤 | 预期 | 实际 | 通过 |
|------|------|------|------|
| A3 | step 3 显示 "续跑" 按钮 | | ☐ |
| A5 | step 3 用 deepseek 重跑成功 | | ☐ |
| A7 | audit_log 有 1 条 UPDATE | | ☐ |
| B5 | session.model_id=1 写回 | | ☐ |
| B6 | audit_log before=2 after=1 | | ☐ |
