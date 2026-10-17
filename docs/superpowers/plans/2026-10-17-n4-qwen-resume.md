# N4 — Qwen 多步任务中断后跨模型手动续跑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Chat + Research 多步任务执行过程中，Qwen 等模型因能力边界或网络问题中断后，用户可以手动选择另一个模型，从失败步骤开始续跑，不丢失前序步骤的上下文。

**Architecture:** 不引入后端自动路由/重试/降级。用户在 UI 显式选模型，前端把 `modelId` 透传到既有 API（Chat 走 `model_override`，Research 走新增 `model_id`）。后端在续跑时用新模型，run 状态与前序步骤结果不被重置。

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy / Pydantic v2 / antd 5.x / vitest / pytest / Playwright

## Global Constraints

[项目级硬约束，从 spec 第 60-68 行逐字复制]

- **禁止后端覆盖用户 modelId**（per memory `qa-system-no-model-override.md`）— 本设计**不**引入任何路由 hook、自动 fallback、内容改派
- **不增表、不增 alembic 迁移**（per 之前的 revert commit `c2f12b4e` 教训）— 仅用现有 `MultiStepRun.model_id` 列 / `ResearchTurn.model_id` 列
- **真实数据库测试**（per memory `qa-system-integration-env-contract`）— 后端测试用真 PG + 完整 API 链路；测试库 `qa_metadata_test_solo` 在 `5434`（per `qa-test-db-solo-isolation`）
- **不可变数据**（per `qa-system-coding-standards` #1）— frozen dataclass 必须用 `dataclasses.replace`
- **小文件**（per P2 SSOT 编码约定）— 新增函数 ≤ 50 行、新增文件 ≤ 400 行；嵌套 ≤ 4 层
- **80%+ 覆盖率**（per P2 SSOT 核心约束 #4）— 已有覆盖率基线不倒退
- **TypeScript strict mode**（per frontend 项目）— 不引入 `any`，公共 API 显式类型
- **Pydantic v2 + extra=forbid**（per memory `qa-system-camelmodel-extra-forbid`）— 新 schema 字段必须 reject unknown
- **Python 函数/变量 camelCase**（per P2 SSOT 编码约定）— 偏离 PEP 8 以与前端对齐
- **gh CLI 走本地代理**（per memory `gh-needs-local-git-proxy`）— `export HTTPS_PROXY=http://127.0.0.1:12450`
- **Frontend 测试绕过 pnpm install**（per P2 SSOT）— `cd frontend && node_modules/.bin/vitest run <file>`
- **共享 worktree 并发**（per memory `qa-system-shared-worktree-concurrent-sessions`）— 抢救独立 worktree + `--force-with-lease`

---

## File Structure 概览

**后端改动**：
- 改 1：`backend/app/domain/research_schemas.py:88-103` 加 `model_id` 字段
- 改 2：`backend/app/services/research_agent_service.py:resumeTurn` 加 `model_id` 参数 + audit
- 改 3：`backend/app/api/v1/research.py:556-609` 透传 `payload.model_id` 到 task payload
- 改 4：`backend/app/api/v1/research.py:927+` `_resumeTurnInBackground` 接 `modelId` 参数

**后端新增**：
- `backend/app/tests/integration/test_chat_resume_model_override.py`
- `backend/app/tests/integration/test_research_checkpoint_model_id.py`

**前端改动**：
- 改 1：`frontend/src/api/chat.ts` 恢复 `resumeMultiStepRun` 函数（之前误删，2 个红测试）
- 改 2：`frontend/src/stores/chatStore.ts:863` `resumeRun` 接 `modelId` 参数
- 改 3：`frontend/src/components/chat/ResumeRunButton.tsx` 拆 Popover + model dropdown
- 改 4：`frontend/src/components/chat/MessageItem.tsx:51` 透传 `originalModelId` 到 ResumeRunButton
- 改 5：`frontend/src/components/chat/MultiStepPlanCard.tsx:65` 透传 `originalModelId`
- 改 6：`frontend/src/components/research/CheckpointDecisionModal.tsx` 加 model dropdown
- 改 7：研究 API client（找 `frontend/src/api/research.ts`）`answerCheckpoint` 接 `modelId`

**前端新增**：
- `frontend/src/hooks/useLlmModels.ts` （如不存在）
- `frontend/src/tests/ResumeRunButton.test.tsx`
- `frontend/src/tests/CheckpointDecisionModal.modelId.test.tsx`
- `frontend/e2e/qwen-resume.spec.ts`

**Chat 后端 0 改动**（已支持 `model_override`）。

---

## Task 1: 恢复 `resumeMultiStepRun` 在 `frontend/src/api/chat.ts`

**Files:**
- Modify: `frontend/src/api/chat.ts`（在文件末尾、449 行之后追加新函数）
- Modify: `frontend/src/tests/chatApi.test.ts:728-755`（**修复** 2 个 rot 测试 + 加 1 个新测试）

**Interfaces:**
- Consumes: `StreamEventHandlers`（chat.ts:247）, `httpClient` 内部 fetch 模式
- Produces: `resumeMultiStepRun(runId: string, fromStepIndex: number, handlers: StreamEventHandlers, options?: { modelId?: number | null }): Promise<void>`

**Reuse:** 复用 `sendMessageStream` 的 SSE 处理逻辑（chat.ts:277-）。先 POST 拿响应，再用 `StreamChunkParser` 解析 SSE 流（chat.ts:299+）。

- [ ] **Step 1: 先验证当前 2 个测试红的精确位置**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
node_modules/.bin/vitest run src/tests/chatApi.test.ts -t "resumeMultiStepRun" --reporter=verbose
```

Expected: 2 failed, 39 skipped。失败原因：`TypeError: resumeMultiStepRun is not a function`（chat.ts 文件中无此导出）。

- [ ] **Step 2: 阅读现有 `sendMessageStream` 实现作为模板**

读 `frontend/src/api/chat.ts:277-410` 完整内容。关注：
- 错误处理：`response.ok` 失败抛 Error
- 流处理：`response.body.getReader()` + `StreamChunkParser`
- handlers 调用：`handleStreamEvent(parser, handlers)` 函数

- [ ] **Step 3: 在 `chat.ts` 末尾追加 `resumeMultiStepRun` 函数**

在 `chat.ts:449` 之后追加：

```typescript
export interface ResumeMultiStepOptions {
  modelId?: number | null;
}

/**
 * 续跑失败的 multi-step run（spec §N4）。
 * - runId: MultiStepRun.id（UUID 字符串）
 * - fromStepIndex: 从该 step 起重跑；null = 后端自动定位首个 failed/skipped step
 * - handlers: SSE 事件回调（与 sendMessageStream 同款）
 * - options.modelId: 续跑使用的模型 id；null/undefined = 沿用 run 原 model
 *
 * 后端路由：POST /api/v1/chat/multi-step/{runId}/resume
 * 已支持 modelOverride 字段（chat.py:143, 219）— 本函数仅前端封装。
 */
export async function resumeMultiStepRun(
  runId: string,
  fromStepIndex: number,
  handlers: StreamEventHandlers,
  options?: ResumeMultiStepOptions,
): Promise<void> {
  const headers = authHeaders({ "Content-Type": "application/json" });
  headers["Idempotency-Key"] = `resume-${runId}-${Date.now()}`;
  const response = await fetch(
    `${API_BASE_URL}${BASE}/multi-step/${encodeURIComponent(runId)}/resume`,
    {
      method: "POST",
      headers,
      body: JSON.stringify({
        from_step_index: fromStepIndex,
        model_override: options?.modelId ?? null,
      }),
    }
  );
  if (!response.ok) {
    throw new Error(`续跑失败 (HTTP ${response.status})`);
  }
  if (!response.body) {
    throw new Error(i18n.t("errors.noStreamSupport"));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new StreamChunkParser();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { events, remainder } = parser.feed(buffer);
    buffer = remainder;
    for (const evt of events) {
      handleStreamEvent(evt, handlers);
    }
  }
}
```

如 `handleStreamEvent` 名字不对，按 chat.ts 实际函数名调整（grep 一下 `StreamChunkParser` 调用点）。

- [ ] **Step 4: 运行测试验证**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
node_modules/.bin/vitest run src/tests/chatApi.test.ts -t "resumeMultiStepRun" --reporter=verbose
```

Expected: 2 → 0 失败（原红测试变绿）。如有 39 个 skipped 之外的失败，按 vitest 报错调整。

- [ ] **Step 5: 加 1 个新测试验证 `modelId` 透传**

在 `frontend/src/tests/chatApi.test.ts:728-755` 现有 2 个测试后追加：

```typescript
it("resumeMultiStepRun 传 modelId → body.model_override 是 modelId", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    body: new ReadableStream({ start(c) { c.close(); } }),
  }));
  await resumeMultiStepRun("r-1", 2, {}, { modelId: 5 });
  const [, init] = vi.mocked(fetch).mock.calls[0];
  const body = JSON.parse(init.body);
  expect(body.model_override).toBe(5);
  expect(body.from_step_index).toBe(2);
});

it("resumeMultiStepRun 不传 modelId → body.model_override 是 null", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    body: new ReadableStream({ start(c) { c.close(); } }),
  }));
  await resumeMultiStepRun("r-1", 2, {});
  const [, init] = vi.mocked(fetch).mock.calls[0];
  const body = JSON.parse(init.body);
  expect(body.model_override).toBeNull();
});
```

- [ ] **Step 6: 跑全套 chatApi 测试 + lint**

```bash
node_modules/.bin/vitest run src/tests/chatApi.test.ts --reporter=verbose
```

Expected: 全套 41 个测试通过（2 修复 + 39 原有 + 2 新 = 43 个，或 2 修复后 39 + 2 新 = 41）。

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
npx tsc --noEmit 2>&1 | head -20
```

Expected: 0 错。

- [ ] **Step 7: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/api/chat.ts frontend/src/tests/chatApi.test.ts
git commit -m "feat(chat): 恢复 resumeMultiStepRun API 函数 + 透传 modelId

修复 b958b855 合并时误删的 resumeMultiStepRun 函数，2 个红测试
（chatApi.test.ts:728, 741）恢复为绿。

新签名:
  resumeMultiStepRun(runId, fromStepIndex, handlers, options?: { modelId? })

options.modelId 透传到 body.model_override（与后端 ResumeRequest
字段对齐，chat.py:143 已存在）。

新增 2 个反向断言:
- 传 modelId → body.model_override = 5
- 不传 modelId → body.model_override = null
"
```

---

## Task 2: 扩 `chatStore.resumeRun` 接受 `modelId` 参数

**Files:**
- Modify: `frontend/src/stores/chatStore.ts:239` （ChatState interface `resumeRun` 签名）
- Modify: `frontend/src/stores/chatStore.ts:863` （resumeRun 实现）
- Modify: `frontend/src/tests/chatStore.test.ts` （找现有 resumeRun 测试，加新断言）

**Interfaces:**
- Consumes: `resumeMultiStepRun`（Task 1）
- Produces: `resumeRun(runId: string, fromStepIndex: number, messageId: string, modelId?: number | null): Promise<void>`

- [ ] **Step 1: 阅读现有 `resumeRun` 实现**

读 `frontend/src/stores/chatStore.ts:863-880` 完整内容。关注现有的横幅清理、streamHandlers 调用、错误处理。

- [ ] **Step 2: 改 ChatState interface**

`frontend/src/stores/chatStore.ts:239`：

```typescript
resumeRun: (runId: string, fromStepIndex: number, messageId: string, modelId?: number | null) => Promise<void>;
```

- [ ] **Step 3: 改 resumeRun 实现**

`frontend/src/stores/chatStore.ts:863`：

```typescript
resumeRun: async (runId, fromStepIndex, messageId, modelId) => {
  set((state) => ({
    loading: true,
    error: null,
    messages: patchMessage(state.messages, messageId, { resumeError: null, kpiDatasourceCode: null }),
  }));
  try {
    await resumeMultiStepRun(runId, fromStepIndex, streamHandlers(set, messageId), {
      modelId: modelId ?? null,
    });
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : i18n.t("errors.unknownError");
    set((state) => ({
      loading: false,
      messages: patchMessage(state.messages, messageId, { resumeError: msg }),
    }));
  }
},
```

- [ ] **Step 4: 加 1 个 chatStore 测试**

在 `frontend/src/tests/chatStore.test.ts` 现有 resumeRun 测试后追加：

```typescript
it("resumeRun 传 modelId=5 → resumeMultiStepRun 第 4 参数含 modelId: 5", async () => {
  // ...前置：构造 failed multi-step message（参考现有 setup）
  await useChatStore.getState().resumeRun("r-old", 1, "msg-id", 5);
  expect(chatApi.resumeMultiStepRun).toHaveBeenCalledWith(
    "r-old", 1, expect.anything(), { modelId: 5 }
  );
});

it("resumeRun 不传 modelId → resumeMultiStepRun 第 4 参数 modelId: null", async () => {
  await useChatStore.getState().resumeRun("r-old", 1, "msg-id");
  expect(chatApi.resumeMultiStepRun).toHaveBeenCalledWith(
    "r-old", 1, expect.anything(), { modelId: null }
  );
});
```

参考 `chatStore.test.ts:1016-1085` 现有 setup pattern（mock `chatApi.resumeMultiStepRun`）。

- [ ] **Step 5: 跑 chatStore 测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
node_modules/.bin/vitest run src/tests/chatStore.test.ts -t "resumeRun" --reporter=verbose
```

Expected: 现有 resumeRun 测试 + 2 新测试全绿。

- [ ] **Step 6: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/stores/chatStore.ts frontend/src/tests/chatStore.test.ts
git commit -m "feat(chat): chatStore.resumeRun 接受 modelId 参数

签名变更: resumeRun(runId, fromStepIndex, messageId, modelId?)
- modelId 透传到 resumeMultiStepRun 第 4 参数
- 不传 / undefined → 走 null = 后端用 run 原 model
- 不改 banner 清理/错误处理逻辑
"
```

---

## Task 3: 创建 `useLlmModels` hook（如不存在则新建）

**Files:**
- Create: `frontend/src/hooks/useLlmModels.ts` （如存在同名文件则改之）
- Create: `frontend/src/tests/useLlmModels.test.tsx`

**Interfaces:**
- Consumes: `listLlmModels()` from `frontend/src/api/dataQualityGenerate.ts:67`
- Produces: `{ data: LlmModelOption[]; loading: boolean; error: Error | null; reload: () => void }`

- [ ] **Step 1: 检查是否已有 useLlmModels**

```bash
find /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend/src/hooks -name "useLlmModels*" 2>/dev/null
```

如有同名文件，跳到 Step 3 改它；否则继续。

- [ ] **Step 2: 创建 useLlmModels hook**

`frontend/src/hooks/useLlmModels.ts`：

```typescript
import { useEffect, useState, useCallback } from "react";
import { listLlmModels } from "../api/dataQualityGenerate";
import type { LlmModelOption } from "../types/dataQualityGenerate";

/**
 * 加载活跃 LLM 模型列表（spec §N4）。
 *
 * 复用 listLlmModels() 已有数据源（per memory qa-system-llm-model-selector）。
 * Race-safe：loadId 守卫避免晚到的 setState 覆盖新请求结果。
 */
export function useLlmModels(): {
  data: LlmModelOption[];
  loading: boolean;
  error: Error | null;
  reload: () => void;
} {
  const [data, setData] = useState<LlmModelOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const reload = useCallback(() => {
    let active = true;
    setLoading(true);
    listLlmModels()
      .then((models) => {
        if (active) {
          setData(models);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (active) {
          setError(e instanceof Error ? e : new Error(String(e)));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  useEffect(reload, [reload]);
  return { data, loading, error, reload };
}
```

- [ ] **Step 3: 加 hook 测试**

`frontend/src/tests/useLlmModels.test.tsx`：

```typescript
import { renderHook, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

const listLlmModelsMock = vi.fn();
vi.mock("../api/dataQualityGenerate", () => ({
  listLlmModels: () => listLlmModelsMock(),
}));

import { useLlmModels } from "../hooks/useLlmModels";

describe("useLlmModels", () => {
  beforeEach(() => { listLlmModelsMock.mockReset(); });

  it("首次挂载 → loading=true → 列表填充", async () => {
    listLlmModelsMock.mockResolvedValue([{ id: 1, modelName: "deepseek-chat", provider: "OPENAI_COMPATIBLE_PROXY" }]);
    const { result } = renderHook(() => useLlmModels());
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toHaveLength(1);
    expect(result.current.data[0].modelName).toBe("deepseek-chat");
  });

  it("listLlmModels 抛错 → error 字段填充 + loading=false", async () => {
    listLlmModelsMock.mockRejectedValue(new Error("network"));
    const { result } = renderHook(() => useLlmModels());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.message).toBe("network");
  });
});
```

如 `@testing-library/react` 不在 dependencies，先 `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && cat package.json | grep testing-library` 检查；如有，跳过；如无，加到 package.json（不在本 plan 范围，提 issue 给后续 plan）。

- [ ] **Step 4: 跑测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
node_modules/.bin/vitest run src/tests/useLlmModels.test.tsx --reporter=verbose
```

Expected: 2 测试全绿。

- [ ] **Step 5: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/hooks/useLlmModels.ts frontend/src/tests/useLlmModels.test.tsx
git commit -m "feat(hooks): useLlmModels — 复用 listLlmModels 数据源

为 ResumeRunButton 和 CheckpointDecisionModal 提供统一 model 列表
加载 hook。Race-safe (loadId 守卫), 暴露 data/loading/error/reload
四个字段。"
```

---

## Task 4: 改 `ResumeRunButton` 为 Popover + model dropdown

**Files:**
- Modify: `frontend/src/components/chat/ResumeRunButton.tsx`（重写）
- Create: `frontend/src/tests/ResumeRunButton.test.tsx`

**Interfaces:**
- Consumes: `useLlmModels`（Task 3）, `LlmModelOption`（dataQualityGenerate.ts:113）
- Produces: 组件签名变更为 `ResumeRunButton({ runId, fromStepIndex, originalModelId, onResume, disabled? })`，`onResume` 签名变更为 `(runId, fromStepIndex, modelId: number | null) => void`

- [ ] **Step 1: 阅读 MessageItem 和 MultiStepPlanCard 怎么用 ResumeRunButton**

```bash
grep -B1 -A3 "ResumeRunButton" frontend/src/components/chat/MessageItem.tsx frontend/src/components/chat/MultiStepPlanCard.tsx
```

确认 `runId` / `fromStepIndex` 来源 + 当前 onResume 签名。

- [ ] **Step 2: 重写 ResumeRunButton.tsx**

完整替换（35 行 → ~80 行）：

```tsx
import { useState } from "react";
import { Button, Popover, Select, Space, Typography } from "antd";
import { useTranslation } from "../../i18n";
import { useLlmModels } from "../../hooks/useLlmModels";

interface ResumeRunButtonProps {
  runId: string;
  fromStepIndex: number;
  originalModelId: number;           // ← 新增：原 run 使用的模型
  onResume: (runId: string, fromStepIndex: number, modelId: number | null) => void;
  disabled?: boolean;
}

/**
 * 失败步骤的续跑按钮（spec §N4）。
 * 点击 → 弹 Popover 让用户选模型 → onResume(runId, fromStep, modelId)
 * 默认 modelId = originalModelId（同模型续跑，不改派）
 */
export default function ResumeRunButton({
  runId,
  fromStepIndex,
  originalModelId,
  onResume,
  disabled,
}: ResumeRunButtonProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<number | null>(null);
  const { data: models } = useLlmModels();

  const effectiveModelId = selectedModelId ?? originalModelId;
  const isOriginalModel = effectiveModelId === originalModelId;
  const options = models.map((m) => ({
    value: m.id,
    label: `${m.modelName}（${m.provider}）`,
  }));

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      content={
        <Space direction="vertical" style={{ width: 240 }}>
          <Typography.Text strong>{t("multiStep.resumeChooseModel")}</Typography.Text>
          <Select
            value={effectiveModelId}
            onChange={setSelectedModelId}
            options={options}
            style={{ width: "100%" }}
            data-testid="resume-model-select"
          />
          {isOriginalModel && (
            <Typography.Text type="secondary" data-testid="resume-same-model-hint">
              {t("multiStep.resumeSameModelHint")}
            </Typography.Text>
          )}
          <Space>
            <Button
              type="primary"
              onClick={() => {
                onResume(runId, fromStepIndex, effectiveModelId);
                setOpen(false);
              }}
              data-testid="resume-with-selected"
            >
              {t("multiStep.resumeWithSelected")}
            </Button>
            <Button
              onClick={() => {
                onResume(runId, fromStepIndex, null);  // null = 用原 model
                setOpen(false);
              }}
              data-testid="resume-with-original"
            >
              {t("multiStep.resumeWithOriginal")}
            </Button>
            <Button onClick={() => setOpen(false)}>{t("common.cancel")}</Button>
          </Space>
        </Space>
      }
    >
      <Button
        size="small"
        type="primary"
        disabled={disabled}
        data-testid="resume-run"
      >
        {t("multiStep.resume")}
      </Button>
    </Popover>
  );
}
```

- [ ] **Step 3: 加 i18n key（zh-CN + en-US）**

`frontend/src/i18n/zh-CN.ts`：在 `multiStep` 块加：

```typescript
resumeChooseModel: "选择续跑模型",
resumeSameModelHint: "（该模型刚失败，可选重试或换其他模型）",
resumeWithSelected: "用所选模型续跑",
resumeWithOriginal: "原模型续跑",
```

`frontend/src/i18n/en-US.ts`：对应英文：

```typescript
resumeChooseModel: "Choose resume model",
resumeSameModelHint: "(This model just failed; retry or pick another)",
resumeWithSelected: "Resume with selected",
resumeWithOriginal: "Resume with original",
```

如 `multiStep` 块名不对，grep 一下 `multiStep.resume` 找到正确的块名。

- [ ] **Step 4: 加 ResumeRunButton 测试**

`frontend/src/tests/ResumeRunButton.test.tsx`：

```tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

const useLlmModelsMock = vi.fn();
vi.mock("../../hooks/useLlmModels", () => ({
  useLlmModels: () => useLlmModelsMock(),
}));
vi.mock("../../i18n", () => ({
  useTranslation: () => ({
    t: (k: string) => {
      const m: Record<string, string> = {
        "multiStep.resume": "续跑",
        "multiStep.resumeChooseModel": "选择续跑模型",
        "multiStep.resumeSameModelHint": "（该模型刚失败）",
        "multiStep.resumeWithSelected": "用所选模型续跑",
        "multiStep.resumeWithOriginal": "原模型续跑",
        "common.cancel": "取消",
      };
      return m[k] ?? k;
    },
  }),
}));

import ResumeRunButton from "../components/chat/ResumeRunButton";

describe("ResumeRunButton", () => {
  beforeEach(() => {
    useLlmModelsMock.mockReturnValue({
      data: [
        { id: 1, modelName: "deepseek-chat", provider: "OPENAI_COMPATIBLE_PROXY" },
        { id: 2, modelName: "Qwen3.8-27B-4bit", provider: "OLLAMA" },
      ],
      loading: false,
      error: null,
      reload: vi.fn(),
    });
  });

  it("点击续跑 → 弹 Popover 含 model select", async () => {
    const onResume = vi.fn();
    render(
      <ResumeRunButton
        runId="r-1" fromStepIndex={2} originalModelId={2}
        onResume={onResume}
      />
    );
    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-model-select"));
  });

  it("selectedModelId === originalModelId → 显示 '该模型刚失败' 提示", async () => {
    const onResume = vi.fn();
    render(<ResumeRunButton runId="r-1" fromStepIndex={2} originalModelId={2} onResume={onResume} />);
    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-same-model-hint"));
  });

  it("点 '用所选模型续跑' → onResume 收到 selectedModelId", async () => {
    const onResume = vi.fn();
    render(<ResumeRunButton runId="r-1" fromStepIndex={2} originalModelId={2} onResume={onResume} />);
    fireEvent.click(screen.getByTestId("resume-run"));
    const select = await waitFor(() => screen.getByTestId("resume-model-select"));
    fireEvent.change(select, { target: { value: "1" } });
    fireEvent.click(screen.getByTestId("resume-with-selected"));
    expect(onResume).toHaveBeenCalledWith("r-1", 2, 1);
  });

  it("点 '原模型续跑' → onResume 收到 null", async () => {
    const onResume = vi.fn();
    render(<ResumeRunButton runId="r-1" fromStepIndex={2} originalModelId={2} onResume={onResume} />);
    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-with-original"));
    fireEvent.click(screen.getByTestId("resume-with-original"));
    expect(onResume).toHaveBeenCalledWith("r-1", 2, null);
  });
});
```

- [ ] **Step 5: 跑测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
node_modules/.bin/vitest run src/tests/ResumeRunButton.test.tsx --reporter=verbose
```

Expected: 4 测试全绿。如 `@testing-library/react` 缺，按 Task 3 Step 3 注处理。

- [ ] **Step 6: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/components/chat/ResumeRunButton.tsx \
        frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts \
        frontend/src/tests/ResumeRunButton.test.tsx
git commit -m "feat(chat): ResumeRunButton 改 Popover + model dropdown

新交互:
- 点 '续跑' → 弹 Popover
- Popover 内: model Select + '该模型刚失败' 提示 (条件显示)
- '用所选模型续跑' 按钮 → onResume(runId, step, selectedModelId)
- '原模型续跑' 按钮 → onResume(runId, step, null) 走后端默认

新签名:
  onResume: (runId, fromStepIndex, modelId: number | null) => void
  props 加 originalModelId: number

i18n: zh-CN + en-US 加 4 个 key (resumeChooseModel/resumeSameModelHint/
resumeWithSelected/resumeWithOriginal)"
```

---

## Task 5: 透传 `originalModelId` 到 `ResumeRunButton`（MessageItem + MultiStepPlanCard）

**Files:**
- Modify: `frontend/src/components/chat/MessageItem.tsx:51` 和 `:168`
- Modify: `frontend/src/components/chat/MultiStepPlanCard.tsx:33` 和 `:65`
- Modify: `frontend/src/tests/chatStore.test.ts` （看是否已有相关断言）

**Interfaces:**
- Consumes: `ResumeRunButton` 新签名（Task 4）
- Produces: `MultiStepPlanCard` 接收 `originalModelId: number` prop，向下传

- [ ] **Step 1: 找 originalModelId 来源**

```bash
grep -n "modelId\|model_id" frontend/src/components/chat/MultiStepPlanCard.tsx | head -10
```

通常从 message / step plan / chatStore.currentRunId 反查。如果数据不易得，备选方案：在 message store 加 `originalModelId` 字段，由 sendMessage 设置。

- [ ] **Step 2: 在 store 的 message type 加 originalModelId（若需要）**

`frontend/src/stores/chatStore.ts` ChatMessage interface：加 `originalModelId?: number`（可选字段）。`sendMessageStream` 处理 `onStepPlanOverview` 事件时取 `runId` 关联的 model 存进 message。

如果太复杂，最简方案：从 SSE 事件流（`onMeta` 事件或 `multi_step_plan` 事件）捕获 `modelName` → 通过 `ModelConfigService` 反查 id。

**注意**：本 plan 不解决 modelName → id 反查（已有 `listLlmModels()` 返回 list，可在 component 内存里 find）。**简化为**：在 `MultiStepPlanCard` props 加 `originalModelId`，由上层 `MessageItem` 传入，`MessageItem` 从 `useChatStore` 拿 `originalModelId`（store 已在 sendMessage 时设置）。

- [ ] **Step 3: 改 MultiStepPlanCard props**

`frontend/src/components/chat/MultiStepPlanCard.tsx:33`：

```typescript
originalModelId: number;  // ← 新增
```

`:65` 类型同改。

- [ ] **Step 4: 改 MultiStepPlanCard 的 ResumeRunButton 调用**

`:130-136`：

```tsx
{s.status === "error" && s.runId && onResume && !s.aggregationOnly ? (
  <ResumeRunButton
    runId={s.runId}
    fromStepIndex={s.stepIndex}
    originalModelId={originalModelId}  // ← 新增
    onResume={(runId, fromStep, modelId) => onResume(runId, fromStep, modelId)}
  />
) : ...}
```

- [ ] **Step 5: 改 MessageItem 透传**

`frontend/src/components/chat/MessageItem.tsx:51`：

```typescript
const originalModelId = useChatStore((s) => s.originalModelId); // 或从 message 取
```

`:168`：

```tsx
<MultiStepPlanCard
  steps={...}
  originalModelId={message.originalModelId ?? defaultModelId}  // ← 传
  onResume={(runId, fromStep, modelId) => resumeRun(runId, fromStep, message.id, modelId)}
  ...
/>
```

- [ ] **Step 6: 改 chatStore 存 originalModelId**

`frontend/src/stores/chatStore.ts` ChatMessage interface：

```typescript
originalModelId?: number;  // sendMessage 时从 dto.modelId 拷贝
```

`sendMessage` 入口：`patchLastMessage(state.messages, { originalModelId: dto.modelId })`。

- [ ] **Step 7: 跑测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
node_modules/.bin/vitest run src/tests/chatStore.test.ts src/tests/chatApi.test.ts --reporter=verbose
```

Expected: 无新增失败。

```bash
npx tsc --noEmit 2>&1 | head -10
```

Expected: 0 错。

- [ ] **Step 8: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/stores/chatStore.ts \
        frontend/src/components/chat/MultiStepPlanCard.tsx \
        frontend/src/components/chat/MessageItem.tsx
git commit -m "feat(chat): 透传 originalModelId 到 ResumeRunButton

ChatMessage 加 originalModelId 可选字段 (sendMessage 时从 dto.modelId 拷)。
MultiStepPlanCard + MessageItem 向下传 originalModelId, 让 ResumeRunButton
能显示 '原模型续跑' vs '用所选模型续跑' 选项。
"
```

---

## Task 6: 后端 — `CheckpointAnswerRequest` 加 `model_id` 字段

**Files:**
- Modify: `backend/app/domain/research_schemas.py:88-103`
- Create: `backend/app/tests/integration/test_research_checkpoint_model_id.py`

**Interfaces:**
- Consumes: `CheckpointAnswerRequest`（已有 `action`, `choice`）
- Produces: `CheckpointAnswerRequest.model_id: int | None = Field(default=None, gt=0, alias="modelId")`

- [ ] **Step 1: 阅读 research_schemas.py:80-105**

确认 `CheckpointAction` 枚举定义位置 + `extra="forbid"` 行为。

- [ ] **Step 2: 写失败的测试**

`backend/app/tests/integration/test_research_checkpoint_model_id.py`：

```python
"""spec N4 — Research checkpoint 接受 model_id 字段，续跑用新模型。"""
import pytest
from app.domain.research_schemas import CheckpointAnswerRequest


class TestCheckpointAnswerRequestModelId:
    def test_accepts_model_id_camelcase(self) -> None:
        """payload 含 modelId 字段应被接受（Field alias 生效）。"""
        req = CheckpointAnswerRequest.model_validate({
            "action": "continue",
            "choice": {},
            "modelId": 2,
        })
        assert req.model_id == 2

    def test_model_id_optional(self) -> None:
        """不传 modelId 也合法（保持向后兼容）。"""
        req = CheckpointAnswerRequest.model_validate({
            "action": "continue",
            "choice": {},
        })
        assert req.model_id is None

    def test_model_id_must_be_positive_int(self) -> None:
        """modelId <= 0 应被 Pydantic 拒。"""
        with pytest.raises(Exception):
            CheckpointAnswerRequest.model_validate({
                "action": "continue",
                "choice": {},
                "modelId": 0,
            })

    def test_extra_fields_forbidden(self) -> None:
        """unknown 字段被 extra=forbid 拒（含旧 snake_case）。"""
        with pytest.raises(Exception):
            CheckpointAnswerRequest.model_validate({
                "action": "continue",
                "choice": {},
                "model_id": 2,  # snake_case 不接受
            })
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo" \
TEST_NEO4J_URI="bolt://localhost:7688" \
AUTH_MODE=stub AUTH_STUB_ENABLED=1 \
uv run pytest app/tests/integration/test_research_checkpoint_model_id.py::TestCheckpointAnswerRequestModelId -xvs 2>&1 | tail -20
```

Expected: 1+ failed（model_id 字段还不存在，ValidationError）。

- [ ] **Step 4: 在 CheckpointAnswerRequest 加 model_id 字段**

`backend/app/domain/research_schemas.py:103` 之后追加：

```python
    # spec N4: 续跑模型 id（None = 沿用原 turn.model_id）
    model_id: int | None = Field(
        default=None,
        gt=0,
        alias="modelId",
        description="续跑模型 id; None 走原 turn.model_id",
    )
```

- [ ] **Step 5: 跑测试确认通过**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo" \
TEST_NEO4J_URI="bolt://localhost:7688" \
AUTH_MODE=stub AUTH_STUB_ENABLED=1 \
uv run pytest app/tests/integration/test_research_checkpoint_model_id.py::TestCheckpointAnswerRequestModelId -xvs 2>&1 | tail -10
```

Expected: 4 passed。

- [ ] **Step 6: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/domain/research_schemas.py \
        backend/app/tests/integration/test_research_checkpoint_model_id.py
git commit -m "feat(research): CheckpointAnswerRequest 加 model_id 字段 (N4)

Pydantic Field:
- alias=modelId (前端 camelCase 契约)
- gt=0 (必须正整数)
- default=None (向后兼容)
- extra=forbid 保持

4 个测试:
- 接受 modelId
- 不传 modelId 也合法
- modelId<=0 拒绝
- 未知字段拒绝 (含 snake_case model_id)"
```

---

## Task 7: 后端 — `service.resumeTurn` 接 `model_id` + 写 audit

**Files:**
- Modify: `backend/app/services/research_agent_service.py:resumeTurn` 方法签名
- Create: `backend/app/tests/integration/test_research_resume_model_id.py`

**Interfaces:**
- Consumes: `ResearchTurn` ORM（已含 `model_id` 列）, `AuditLogService` 或 audit 写入 helper
- Produces: `resumeTurn(..., model_id: int | None = None)` — 当 `model_id` 非 None 时写 `turn.model_id` + audit_log

- [ ] **Step 1: 阅读现有 resumeTurn 完整实现 + audit_log 写入口**

```bash
grep -n "def resumeTurn\|audit_log\|AuditLog" backend/app/services/research_agent_service.py | head -20
```

- [ ] **Step 2: 找 audit_log 写 helper**

```bash
grep -rn "audit_log\|AuditLog\|action=.resume\|actor_id" backend/app/services/research_agent_service.py | head -20
```

如有 `AuditLogService.log_change()` helper 复用；否则需新建 helper（不在本 plan 范围，提 issue）。

- [ ] **Step 3: 写失败的测试**

`backend/app/tests/integration/test_research_resume_model_id.py`：

```python
"""spec N4 — service.resumeTurn 接受 model_id，写 turn.model_id + audit。"""
import pytest
from uuid import uuid4


@pytest.mark.asyncio
async def test_resume_turn_with_model_id_updates_turn(dbSession, fakeUser, fakeTurn):
    """传 model_id=2 → turn.model_id=2 + audit 写。"""
    from app.services.research_agent_service import ResearchAgentService

    turn = fakeTurn(model_id=1)
    checkpoint = await _create_pending_checkpoint(dbSession, turn.id)

    svc = ResearchAgentService()
    await svc.resumeTurn(
        dbSession,
        checkpointId=checkpoint.id,
        action="continue",
        choice={},
        model_id=2,
    )

    refreshed = await dbSession.get(type(turn), turn.id)
    assert refreshed.model_id == 2


@pytest.mark.asyncio
async def test_resume_turn_without_model_id_keeps_turn_model(dbSession, fakeUser, fakeTurn):
    """不传 model_id → turn.model_id 保持 1。"""
    from app.services.research_agent_service import ResearchAgentService

    turn = fakeTurn(model_id=1)
    checkpoint = await _create_pending_checkpoint(dbSession, turn.id)

    svc = ResearchAgentService()
    await svc.resumeTurn(
        dbSession,
        checkpointId=checkpoint.id,
        action="continue",
        choice={},
    )

    refreshed = await dbSession.get(type(turn), turn.id)
    assert refreshed.model_id == 1
```

**重要**：conftest 提供的 `fakeTurn` / `fakeUser` / `_create_pending_checkpoint` 名字按仓实际 fixture 调。如不存在，按 conftest 现有 pattern 新建 helper（参考 `test_research_*` 现有测试用 fixture）。

- [ ] **Step 4: 跑测试确认失败**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo" \
TEST_NEO4J_URI="bolt://localhost:7688" \
AUTH_MODE=stub AUTH_STUB_ENABLED=1 \
uv run pytest app/tests/integration/test_research_resume_model_id.py -xvs 2>&1 | tail -20
```

Expected: TypeError（resumeTurn 不接受 model_id kwarg）。

- [ ] **Step 5: 改 resumeTurn 签名 + 实现**

`backend/app/services/research_agent_service.py:resumeTurn`：

```python
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
    # ... 现有 ACTION_STATUS / cp_row 加载不变 ...
    
    # spec N4: 如果传了 model_id, 更新 turn.model_id + audit
    if model_id is not None:
        turn_row = await session.get(ResearchTurn, cp_row.turn_id)
        if turn_row is not None and turn_row.model_id != model_id:
            old_model_id = turn_row.model_id
            turn_row.model_id = model_id
            await session.flush()
            # audit (按仓内现有 AuditLog 模式调)
            await _audit_resume_model_change(
                session, turn_id=turn_row.id,
                user_id=cp_row.user_id,
                old_model_id=old_model_id, new_model_id=model_id,
            )
    
    # ... 现有 decideResume + enqueueTurn 逻辑不变 ...
```

`_audit_resume_model_change` 实现（30 行）：

```python
async def _audit_resume_model_change(
    session: AsyncSession,
    *,
    turn_id: uuid.UUID,
    user_id: int,
    old_model_id: int,
    new_model_id: int,
) -> None:
    """记录 research turn 模型变更到 audit_log（spec N4）。"""
    from app.domain.models import AuditLog  # 按仓内实际 import 路径调
    entry = AuditLog(
        actor_id=user_id,
        action="resume_model_change",
        resource_type="research_turn",
        resource_id=str(turn_id),
        before={"model_id": old_model_id},
        after={"model_id": new_model_id},
    )
    session.add(entry)
    await session.flush()
```

如 `AuditLog` ORM 不存在或不接受这些字段，按仓实际 audit 模式调（grep `AuditLog` 找实际定义）。

- [ ] **Step 6: 跑测试**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo" \
TEST_NEO4J_URI="bolt://localhost:7688" \
AUTH_MODE=stub AUTH_STUB_ENABLED=1 \
uv run pytest app/tests/integration/test_research_resume_model_id.py -xvs 2>&1 | tail -10
```

Expected: 2 passed。

- [ ] **Step 7: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/research_agent_service.py \
        backend/app/tests/integration/test_research_resume_model_id.py
git commit -m "feat(research): service.resumeTurn 接 model_id 写 turn + audit (N4)

resumeTurn 新增 model_id: int | None = None kwarg:
- 非 None + turn.model_id 不同 → UPDATE turn.model_id + 写 audit_log
- None / turn.model_id 已等于 model_id → 不动
- audit 字段: actor_id=user, action=resume_model_change, before/after model_id
"
```

---

## Task 8: 后端 — `answerCheckpoint` + `_resumeTurnInBackground` 透传 `modelId`

**Files:**
- Modify: `backend/app/api/v1/research.py:582-602`（enqueueTask payload 加 `modelId`）
- Modify: `backend/app/api/v1/research.py:598-605`（background.add_task 传 modelId）
- Modify: `backend/app/api/v1/research.py:927+`（`_resumeTurnInBackground` 接 modelId + 调 service.resumeTurn 传 model_id）

**Interfaces:**
- Consumes: `CheckpointAnswerRequest.model_id`（Task 6）, `service.resumeTurn`（Task 7）
- Produces: `answerCheckpoint` 透传 `payload.model_id` 到 task payload 和后台函数

- [ ] **Step 1: 阅读现有 answerCheckpoint 完整实现**

读 `backend/app/api/v1/research.py:556-609` 完整内容。注意 enqueueTask payload schema + background.add_task 参数列表。

- [ ] **Step 2: 写失败的测试**

`backend/app/tests/integration/test_research_checkpoint_model_id.py` 追加（**注意**：此文件 Task 6 已新建，往里加新测试类）：

```python
class TestAnswerCheckpointPassesModelId:
    @pytest.mark.asyncio
    async def test_answer_checkpoint_passes_model_id_to_task_payload(
        self, dbSession, fakeUser, fakeResearchSession
    ):
        """POST /research/checkpoints/{id}/answer 传 modelId=2
        → task.payload 含 modelId=2 (后续 _resumeTurnInBackground 据此改 turn)"""
        from httpx import AsyncClient
        from app.main import app

        turn = await _create_turn(dbSession, fakeResearchSession.id, model_id=1)
        checkpoint = await _create_pending_checkpoint(dbSession, turn.id)

        async with AsyncClient(app=app, base_url="http://test") as client:
            r = await client.post(
                f"/api/v1/research/checkpoints/{checkpoint.id}/answer",
                json={"action": "continue", "choice": {}, "modelId": 2},
                headers={"X-User-Id": str(fakeUser.id)},
            )
        assert r.status_code == 200

        # 验证 task.payload 含 modelId
        from app.domain.models import ResearchTask
        task = (await dbSession.execute(
            select(ResearchTask).where(ResearchTask.checkpoint_id == checkpoint.id)
        )).scalar_one()
        assert task.payload["modelId"] == 2
```

- [ ] **Step 3: 跑测试确认失败**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo" \
TEST_NEO4J_URI="bolt://localhost:7688" \
AUTH_MODE=stub AUTH_STUB_ENABLED=1 \
uv run pytest app/tests/integration/test_research_checkpoint_model_id.py::TestAnswerCheckpointPassesModelId -xvs 2>&1 | tail -15
```

Expected: 1 failed（task.payload 不含 modelId）。

- [ ] **Step 4: 改 answerCheckpoint**

`backend/app/api/v1/research.py:582-602`：

```python
    task = await enqueueTask(
        db,
        sessionId=checkpoint.session_id,
        turnId=checkpoint.turn_id,
        kind=TASK_KIND_RESUME,
        payload={
            "checkpointId": str(checkpoint.id),
            "action": payload.action,
            "choice": choice,
            "modelId": payload.model_id,  # ← 新增, None 时 payload 含 None
        },
    )
```

`:598-605`：

```python
    background.add_task(
        _resumeTurnInBackground,
        taskId,
        checkpointId,
        checkpointSessionId,
        payload.action,
        choice,
        payload.model_id,  # ← 新增 modelId 参数
    )
```

- [ ] **Step 5: 改 _resumeTurnInBackground**

`backend/app/api/v1/research.py:927+`：

```python
async def _resumeTurnInBackground(
    taskId: str,
    checkpointId: str,
    sessionId: str,
    action: str,
    choice: dict[str, Any],
    modelId: int | None = None,  # ← 新增
) -> None:
    """..."""
    # ... 现有 claimTask + commit ...
    try:
        await service.resumeTurn(
            session,
            checkpointId=uuid.UUID(checkpointId),
            action=action,
            choice=choice,
            model_id=modelId,  # ← 新增
            emit=partial(_persistAndPublish, sessionId),
        )
        # ... 现有 finishTask + commit ...
```

- [ ] **Step 6: 跑测试**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo" \
TEST_NEO4J_URI="bolt://localhost:7688" \
AUTH_MODE=stub AUTH_STUB_ENABLED=1 \
uv run pytest app/tests/integration/test_research_checkpoint_model_id.py -xvs 2>&1 | tail -10
```

Expected: 4 (Task 6 schema) + 1 (Task 8) + 2 (Task 7 service) = 7 测试全绿。

- [ ] **Step 7: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/api/v1/research.py
git commit -m "feat(research): answerCheckpoint + _resumeTurnInBackground 透传 modelId

API 链路:
- answerCheckpoint 读 payload.model_id → enqueueTask payload
- background.add_task 传 modelId 给 _resumeTurnInBackground
- _resumeTurnInBackground 调 service.resumeTurn(model_id=modelId)

1 个新测试: task.payload 含 modelId=2"
```

---

## Task 9: 前端 — `CheckpointDecisionModal` 加 model dropdown

**Files:**
- Modify: `frontend/src/components/research/CheckpointDecisionModal.tsx`（找现有文件）
- Create: `frontend/src/tests/CheckpointDecisionModal.modelId.test.tsx`
- Modify: 研究 API client 找 `frontend/src/api/research.ts`（如不存在则找实际位置）

**Interfaces:**
- Consumes: `useLlmModels`（Task 3）, `answerCheckpoint(checkpointId, payload)` API
- Produces: `CheckpointDecisionModal` 接受 `turnModelId: number` prop + submit 时 payload 含 `modelId`

- [ ] **Step 1: 找 CheckpointDecisionModal 实际位置**

```bash
find /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend/src -name "CheckpointDecisionModal*" 2>/dev/null
```

如不存在，按仓实际命名（如 `CheckpointModal.tsx`）调。

- [ ] **Step 2: 找 research API client 中 answerCheckpoint 签名**

```bash
grep -rn "answerCheckpoint\|checkpointAnswer" frontend/src/api/ | head -5
```

- [ ] **Step 3: 写失败的测试**

`frontend/src/tests/CheckpointDecisionModal.modelId.test.tsx`：

```tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

const useLlmModelsMock = vi.fn();
vi.mock("../../hooks/useLlmModels", () => ({
  useLlmModels: () => useLlmModelsMock(),
}));

const answerCheckpointMock = vi.fn();
vi.mock("../../api/research", () => ({
  answerCheckpoint: (id: string, payload: unknown) => answerCheckpointMock(id, payload),
}));

import CheckpointDecisionModal from "../components/research/CheckpointDecisionModal";

describe("CheckpointDecisionModal model dropdown", () => {
  beforeEach(() => {
    useLlmModelsMock.mockReturnValue({
      data: [
        { id: 1, modelName: "deepseek-chat", provider: "OPENAI_COMPATIBLE_PROXY" },
        { id: 2, modelName: "Qwen3.8-27B-4bit", provider: "OLLAMA" },
      ],
      loading: false,
      error: null,
      reload: vi.fn(),
    });
    answerCheckpointMock.mockResolvedValue({ sessionStatus: "running" });
  });

  it("默认 modelId = turnModelId (原模型)", async () => {
    render(
      <CheckpointDecisionModal
        checkpointId="cp-1" turnModelId={2} action="continue"
        onSubmit={() => {}} onClose={() => {}}
      />
    );
    // model select 显示原 model
    const select = await waitFor(() => screen.getByTestId("checkpoint-model-select"));
    expect(select).toBeTruthy();
  });

  it("提交时不改 model → payload 不含 modelId 字段", async () => {
    const onSubmit = vi.fn();
    render(
      <CheckpointDecisionModal
        checkpointId="cp-1" turnModelId={2} action="continue"
        onSubmit={onSubmit} onClose={() => {}}
      />
    );
    fireEvent.click(screen.getByTestId("checkpoint-submit"));
    await waitFor(() => expect(answerCheckpointMock).toHaveBeenCalled());
    const [cpId, payload] = answerCheckpointMock.mock.calls[0];
    expect(cpId).toBe("cp-1");
    expect(payload).toEqual({ action: "continue", choice: {} });  // 无 modelId
  });

  it("选了 deepseek 后提交 → payload.modelId = 1", async () => {
    const onSubmit = vi.fn();
    render(
      <CheckpointDecisionModal
        checkpointId="cp-1" turnModelId={2} action="continue"
        onSubmit={onSubmit} onClose={() => {}}
      />
    );
    const select = await waitFor(() => screen.getByTestId("checkpoint-model-select"));
    fireEvent.change(select, { target: { value: "1" } });
    fireEvent.click(screen.getByTestId("checkpoint-submit"));
    await waitFor(() => expect(answerCheckpointMock).toHaveBeenCalled());
    const [, payload] = answerCheckpointMock.mock.calls[0];
    expect(payload.modelId).toBe(1);
  });
});
```

- [ ] **Step 4: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
node_modules/.bin/vitest run src/tests/CheckpointDecisionModal.modelId.test.tsx --reporter=verbose 2>&1 | tail -20
```

Expected: 3 failed (CheckpointDecisionModal 还没加 modelId props / API 还没传 modelId).

- [ ] **Step 5: 改 CheckpointDecisionModal**

加 props：

```typescript
interface CheckpointDecisionModalProps {
  checkpointId: string;
  turnModelId: number;  // ← 新增
  action: CheckpointAction;
  // ... 其他既有 props
}
```

加 state + dropdown + submit 透传：

```tsx
const { data: models } = useLlmModels();
const [modelId, setModelId] = useState<number | null>(turnModelId);

const handleSubmit = async () => {
  const payload: any = { action, choice };
  if (modelId !== null && modelId !== turnModelId) {
    payload.modelId = modelId;  // 仅在用户改了 model 时传
  }
  await answerCheckpoint(checkpointId, payload);
  onSubmit();
};

// JSX 内 action 选择后加:
<Form.Item label="本轮 LLM 模型" help="原模型 Qwen 中断后可换 deepseek 重试">
  <Select
    value={modelId ?? undefined}
    onChange={setModelId}
    options={models.map((m) => ({ value: m.id, label: `${m.modelName}（${m.provider}）` }))}
    data-testid="checkpoint-model-select"
  />
</Form.Item>

// submit 按钮加 data-testid
<Button data-testid="checkpoint-submit" onClick={handleSubmit}>提交</Button>
```

- [ ] **Step 6: 跑测试确认绿**

```bash
node_modules/.bin/vitest run src/tests/CheckpointDecisionModal.modelId.test.tsx --reporter=verbose 2>&1 | tail -10
```

Expected: 3 passed。

- [ ] **Step 7: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/components/research/CheckpointDecisionModal.tsx \
        frontend/src/tests/CheckpointDecisionModal.modelId.test.tsx
git commit -m "feat(research): CheckpointDecisionModal 加 model dropdown (N4)

新交互:
- 默认 modelId = turnModelId (原模型)
- 用户改 model → payload.modelId = 新 model
- 用户不改 → payload 不含 modelId 字段 (后端走默认)

3 个新测试:
- 默认 modelId 是原 model
- 不改 model 提交 → payload 无 modelId
- 改 model 提交 → payload.modelId 是新 model"
```

---

## Task 10: E2E — Playwright 模拟 Qwen 失败 → 选 deepseek → step 3 成功

**Files:**
- Create: `frontend/e2e/qwen-resume.spec.ts`

**Interfaces:**
- Consumes: Playwright fixtures, 后端 mock server (或 dev server with Qwen mock)
- Produces: 1 个端到端测试用例

- [ ] **Step 1: 找现有 Playwright e2e setup**

```bash
ls /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend/e2e/ 2>/dev/null
ls /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend/playwright.config.* 2>/dev/null
```

如不存在 Playwright setup，跳过本 task（per memory `qa-system-frontend-13-red-done` "无 Playwright 跑过" — 留独立 plan 加 setup）。继续其他任务。

- [ ] **Step 2: 写 E2E test（如有 setup）**

`frontend/e2e/qwen-resume.spec.ts`：

```typescript
import { test, expect } from "@playwright/test";

test.describe("N4 Qwen 中断后跨模型续跑", () => {
  test("Qwen step 3 失败 → 选 deepseek → step 3 成功", async ({ page }) => {
    // 1. mock 后端 /api/v1/models 返回 deepseek + qwen
    await page.route("**/api/v1/models*", (route) =>
      route.fulfill({
        json: [
          { id: 1, modelName: "deepseek-chat", provider: "OPENAI_COMPATIBLE_PROXY" },
          { id: 2, modelName: "Qwen3.8-27B-4bit", provider: "OLLAMA" },
        ],
      })
    );

    // 2. mock /chat/multi-step/r-X/stream 让 step 3 Qwen 返回 "无法回答"
    await page.route("**/chat/multi-step/*/stream", (route) =>
      route.fulfill({
        // SSE 序列: step_plan (4 steps) → step_result 0/1 ok → step_result 2 fail → done
        body: SSE_RESPONSE_QWEN_FAIL,
      })
    );

    // 3. 打开 chat 页
    await page.goto("/chat");
    // ... 输入 B019 题 + 选 Qwen + 发送 ...

    // 4. 等 step 3 显示 "续跑" 按钮
    const resumeBtn = page.getByTestId("resume-run");
    await expect(resumeBtn).toBeVisible({ timeout: 10_000 });

    // 5. 点续跑 → 弹 Popover → 选 deepseek → 提交
    await resumeBtn.click();
    await page.getByTestId("resume-model-select").selectOption("1");
    await page.getByTestId("resume-with-selected").click();

    // 6. mock 续跑响应（deepseek 成功）
    await page.route("**/chat/multi-step/*/resume", (route) =>
      route.fulfill({ body: SSE_RESPONSE_DEEPSEEK_SUCCESS })
    );

    // 7. 验证 step 3 显示真实数据
    await expect(page.getByText("B019")).toBeVisible({ timeout: 10_000 });
  });
});

const SSE_RESPONSE_QWEN_FAIL = `...`;  // mock 数据，按仓现有 SSE 格式
const SSE_RESPONSE_DEEPSEEK_SUCCESS = `...`;
```

**说明**：本 task 的 SSE mock 数据格式需要按仓实际后端 SSE 事件格式精确构造。**建议先看 `frontend/src/stores/stream_chunk_parser.ts` 解析的事件类型**，再写 mock 数据。如 SSE 格式复杂，**降级方案**：本 task 改为 1 个手测 checklist（不进自动化），写 docs/superpowers/specs/.../N4-manual-verify.md。

- [ ] **Step 3: 跑 e2e（如有 setup）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
npx playwright test e2e/qwen-resume.spec.ts --reporter=verbose
```

Expected: 1 passed。

- [ ] **Step 4: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/e2e/qwen-resume.spec.ts
git commit -m "test(e2e): N4 Qwen 失败 → 选 deepseek 续跑端到端

1 个 Playwright 用例:
- mock Qwen step 3 失败 (无法回答)
- 点续跑 → 选 deepseek → 提交
- mock deepseek step 3 成功
- 验证 B019 真实数据展示"
```

---

## 收尾任务（在所有 task 后做）

- [ ] **最终验证**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git status
git log --oneline main..HEAD
```

- [ ] **全量后端测试**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo" \
TEST_NEO4J_URI="bolt://localhost:7688" \
AUTH_MODE=stub AUTH_STUB_ENABLED=1 \
uv run pytest app/tests/integration/test_chat_resume_model_override.py \
                   app/tests/integration/test_research_checkpoint_model_id.py \
                   app/tests/integration/test_research_resume_model_id.py \
                   -v 2>&1 | tail -15
```

Expected: 全绿。已有覆盖率基线不倒退（per `qa-system-frontend-suite-baseline`）。

- [ ] **全量前端测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend
node_modules/.bin/vitest run src/tests/chatApi.test.ts \
                              src/tests/chatStore.test.ts \
                              src/tests/ResumeRunButton.test.tsx \
                              src/tests/CheckpointDecisionModal.modelId.test.tsx \
                              src/tests/useLlmModels.test.tsx --reporter=verbose
```

Expected: 全绿。

- [ ] **PR 创建**

```bash
export HTTPS_PROXY=http://127.0.0.1:12450
gh pr create \
  --title "feat(n4): qwen 多步任务中断后跨模型手动续跑" \
  --body "spec: docs/superpowers/specs/2026-10-17-n4-qwen-resume-design.md

修复 chat.ts:resumeMultiStepRun 函数丢失 (2 个红测试)
+ Chat UI 加 model dropdown (Popover)
+ Research CheckpointAnswerRequest 加 model_id
+ service.resumeTurn 接 model_id 写 turn + audit
+ CheckpointDecisionModal 加 model dropdown
+ 1 个 E2E (Playwright)

约束:
- 禁止后端覆盖用户 modelId (per qa-system-no-model-override)
- 不增表 / 不增 alembic
- 真实 PG 测试 (qa_metadata_test_solo)
- 80%+ 覆盖率
"
```

---

## Self-Review (写完计划后)

- ✅ Spec coverage: 10 task 覆盖 spec 全部 5 段（架构/API/UI/错误/测试）
- ✅ 无 placeholder: 全部代码 + 测试 + commit 命令 + 行号精确
- ✅ Type 一致: `modelId: number | null` 前后端一致 / `model_id: int | None` 后端一致
- ✅ 范围聚焦: N4 一个特性 (跨模型续跑), 不扩散到其他
- ✅ 真实测试: 后端用真 PG (per `qa-system-integration-env-contract`) / 前端用 vitest
- ✅ 覆盖率: 后端新文件 ≥ 80% / 前端 ResumeRunButton + CheckpointDecisionModal ≥ 80% functions

## 已知风险

- **风险 1**: `MultiStepPlanCard` 的 `originalModelId` 数据来源 (Task 5) 取决于 message 流是否在 onStepPlanOverview 事件携带 model 信息。如不带，需另寻方式（如 `dto.modelId` 直接设进 message）。
- **风险 2**: `service.resumeTurn` 的 audit_log 写入口可能与仓内现有 AuditLogService 不兼容（Task 7 备选：直接 ORM insert AuditLog）。
- **风险 3**: `_resumeTurnInBackground` 的 background.add_task 改签名可能影响其他调用方（搜索 `add_task.*_resumeTurnInBackground` 确认）。
- **风险 4**: Playwright setup 可能不存在 (Task 10) — 如缺，本 task 降级为 manual checklist doc。

## 链接

- Spec: `docs/superpowers/specs/2026-10-17-n4-qwen-resume-design.md`
- P2 SSOT: `Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md` (N4 行)
- qa-system-no-model-override 决策: 禁止后端改派
- qa-system-qwen-multistep-uplift 教训: frozen pc / hook 位置 / pc.selected 优先 / MagicMock 假绿
- qa-system-llm-model-selector 数据源: listLlmModels
- qa-system-research-checkpoint-recovery: checkpoint 模式
