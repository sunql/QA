# Checkpoint 处理中 UX 修复 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复研究 checkpoint 确认按钮重复点击触发 409 的 UX bug，并让"处理中"状态更醒目、分析完成后给"查看报告"提示。

**Architecture:** 三处独立可测的改动，全部在 `frontend/`：

1. `CheckpointCard.tsx` — `CheckpointActions` 的 3 个按钮在 `resolutionInFlight` 时 disabled；按钮行下方用独立 banner 显示大号 Spin + 大号"处理中…"文字。
2. `ResearchSessionPage.tsx` — 订阅 store 的 `streaming`/`resolutionInFlight` 变化，在 `streaming: false && resolutionInFlight: false && turns 至少 1 个 && !conflictError` 时弹 antd `notification`，带"查看报告"按钮 navigate 到 `/research/:id/report`。
3. `CheckpointCard.tsx` `submitConfirm` — 提交前 early return 防御（防 SSE 状态慢一拍期间被点），与 disabled 形成双保险。

**Tech Stack:** React 18 + TypeScript + Ant Design 5 (`Spin`, `notification`, `Button`) + Zustand store + i18next（沿用 `research.checkpoint.*` namespace）。

## Global Constraints

- **必须不**改后端、`chat_service`、`research_agent_service`、store 类型定义。
- **必须不**改 `Spin` 之外的 i18n key（沿用 `research.checkpoint.processing` = "处理中…"，如需更醒目文案，新增 1 个 key 而不是覆盖旧 key，保持 i18n 单点修改的契约）。
- **必须**沿用 store 已有的 `resolutionInFlight` 字段（`researchStore.ts:86, 147, 284, 365`），不引入新 state。
- **必须**保持现有 `streaming`（初次 SSE 连接）与 `resolutionInFlight`（提交答案后等待回包）的语义区分。
- **必须**保持 mock 单测不打真后端（`vite test --run`）。
- 不用 `dangerouslySetInnerHTML`；按钮 onClick 不引入异步副作用外的同步重渲染。
- 改动文件控制在 2 个（`CheckpointCard.tsx` + `ResearchSessionPage.tsx`），必要 i18n 改动控制在 `frontend/src/i18n/zh-CN.ts`（如需新增 key）。
- Commit 规范：本仓库 `feat:` / `fix:`，attribution 由 git settings 控制。
- 单文件 200-400 行目标，800 行上限。`CheckpointCard.tsx` 当前约 600 行，新增代码 ≤ 60 行。
- **必须**沿用 antd 的 `notification` API（已在 `ResearchSessionPage.tsx` 中 import 了 `message`，再 import `notification`）。
- 不引入新依赖。

---

## Task 1: 修 `CheckpointCard.tsx` — 按钮 disabled + 处理中 banner

**Files:**
- Modify: `frontend/src/components/research/CheckpointCard.tsx:362-490`（`CheckpointActions` 组件主体 + 末尾的 Spin）
- Test: `frontend/tests/unit/research/CheckpointCard.test.tsx`（如不存在则新建，参考同级组件测试）

**Interfaces:**
- Consumes: `resolutionInFlight: boolean`（已通过 props 传入 CheckpointCard → CheckpointActions；store 字段 `researchStore.resolutionInFlight`）
- Consumes: `onAnswer: (action, choice) => void`（已存在 props）
- Consumes: i18n key `research.checkpoint.processing`（"处理中…"）
- Produces: 3 个 button 的 `disabled={resolutionInFlight}` + 独立 banner（id 锚点 `data-testid="checkpoint-processing-banner"`）

### Step 1: 写失败测试 — 按钮 disabled

打开 `frontend/tests/unit/research/CheckpointCard.test.tsx`（如不存在，参考 `frontend/tests/unit/research/` 既有文件结构新建），写入：

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { CheckpointCard } from "@/components/research/CheckpointCard";
import type { ResearchCheckpoint } from "@/types/research";

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <ConfigProvider locale={zhCN}>
    <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
  </ConfigProvider>
);

const baseCheckpoint: ResearchCheckpoint = {
  id: "66947e24-9bf8-402d-9494-adcc83ca9a14",
  sessionId: "test",
  stage: "verification",
  question: "请确认?",
  options: [
    { key: "A", label: "选项 A" },
    { key: "B", label: "选项 B" },
  ],
  requiredAction: "confirm",
  status: "pending",
  createdAt: new Date().toISOString(),
} as ResearchCheckpoint;

describe("CheckpointCard - resolutionInFlight 状态", () => {
  it("resolutionInFlight=true 时 3 个按钮全部 disabled", () => {
    const onAnswer = vi.fn();
    render(
      <CheckpointCard
        checkpoint={baseCheckpoint}
        onAnswer={onAnswer}
        resolutionInFlight
      />,
      { wrapper },
    );
    const confirmBtn = screen.getByRole("button", { name: /确认/ });
    const modifyBtn = screen.getByRole("button", { name: /修改/ });
    const rejectBtn = screen.getByRole("button", { name: /拒绝/ });
    expect(confirmBtn).toBeDisabled();
    expect(modifyBtn).toBeDisabled();
    expect(rejectBtn).toBeDisabled();
  });

  it("resolutionInFlight=false 时 3 个按钮全部 enabled", () => {
    const onAnswer = vi.fn();
    render(
      <CheckpointCard
        checkpoint={baseCheckpoint}
        onAnswer={onAnswer}
        resolutionInFlight={false}
      />,
      { wrapper },
    );
    const confirmBtn = screen.getByRole("button", { name: /确认/ });
    expect(confirmBtn).not.toBeDisabled();
  });

  it("resolutionInFlight=true 时显示大号处理中 banner", () => {
    const onAnswer = vi.fn();
    const { container } = render(
      <CheckpointCard
        checkpoint={baseCheckpoint}
        onAnswer={onAnswer}
        resolutionInFlight
      />,
      { wrapper },
    );
    const banner = container.querySelector(
      '[data-testid="checkpoint-processing-banner"]',
    );
    expect(banner).toBeInTheDocument();
    // 字号 ≥ 20px，Spin 容器 ≥ 32px
    const style = window.getComputedStyle(banner as Element);
    expect(parseInt(style.fontSize, 10)).toBeGreaterThanOrEqual(20);
  });
});
```

**注意**：实际 props 形态（`CheckpointCard` 是否直接接收 `resolutionInFlight`）需要先查 `CheckpointCard.tsx` 顶部 props 声明。如果 `resolutionInFlight` 是从 store 直接消费（hook 内部），测试需要 wrap `<Provider>` 或改用 hook 模拟。请按实际情况调整 wrapper — 调整后保持"按钮 disabled"这条断言不变（这条是核心契约）。

### Step 2: 跑测试确认失败

```bash
cd frontend
npx vitest run tests/unit/research/CheckpointCard.test.tsx
```

期望：3 个测试全部失败（按钮当前未 disabled、banner 不存在）。

### Step 3: 改 CheckpointCard.tsx

**3a. 给按钮加 `disabled` 属性（行 397-432 三个 Button）**

把：
```tsx
<Button type="primary" onClick={onConfirm}>
  {t("research.checkpoint.confirm")}
</Button>
```
改成：
```tsx
<Button
  type="primary"
  onClick={onConfirm}
  disabled={resolutionInFlight}
  aria-busy={resolutionInFlight}
>
  {t("research.checkpoint.confirm")}
</Button>
```

同理修改"修改"和"拒绝"两个按钮。`resolutionInFlight` 是已有 props（或从 hook 取），直接用。

**3b. `submitConfirm` / `submitModify` / `submitReject` 加 early return 防御（行 444-460）**

```tsx
const submitConfirm = () => {
  if (resolutionInFlight) return;       // 新增：防 SSE 状态延迟期间重复触发
  onAnswer("confirm", selectedIndexes.length > 0 ? { selectedIndexes } : {});
};
```

同理改 `submitModify` / `submitReject`。这一步是 disabled 状态机的双保险（防 focus / keyboard enter 触发的边角）。

**3c. 替换小号 Spin 为独立 banner（行 486-490）**

把：
```tsx
{resolutionInFlight && (
  <Spin size="small" style={{ marginLeft: 12 }}>
    {t("research.checkpoint.processing")}
  </Spin>
)}
```
替换为：
```tsx
{resolutionInFlight && (
  <div
    data-testid="checkpoint-processing-banner"
    role="status"
    aria-live="polite"
    style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      gap: 12,
      marginTop: 16,
      padding: "12px 20px",
      borderRadius: 8,
      background: "rgba(22, 119, 255, 0.06)",
      border: "1px solid rgba(22, 119, 255, 0.25)",
      fontSize: 20,
      fontWeight: 600,
      color: "#1677ff",
    }}
  >
    <Spin size="large" />
    <span style={{ fontSize: 20 }}>{t("research.checkpoint.processing")}</span>
  </div>
)}
```

**字号契约**：`fontSize: 20` 配 `Spin size="large"`（antd Spin large 是 32px 圆环）。

### Step 4: 跑测试确认通过

```bash
cd frontend
npx vitest run tests/unit/research/CheckpointCard.test.tsx
```

期望：3 个测试全部通过。

### Step 5: TypeScript 编译

```bash
cd frontend
npx tsc --noEmit
```

期望：无报错。

### Step 6: Commit

```bash
git add frontend/src/components/research/CheckpointCard.tsx \
        frontend/tests/unit/research/CheckpointCard.test.tsx
git commit -m "fix(research): checkpoint 按钮 disabled + 处理中 banner

- 3 个按钮 resolutionInFlight 时 disabled，aria-busy
- 按钮 onClick 加 early return 防御 SSE 状态延迟
- 替换小号 Spin 为独立 banner（32px Spin + 20px 文字 + 醒目底色）"
```

---

## Task 2: `ResearchSessionPage.tsx` — 分析完成 notification 提示

**Files:**
- Modify: `frontend/src/pages/research/ResearchSessionPage.tsx:50-110`（已有 useEffect 监听 conflictError，新增 useEffect 监听完成态）
- Test: 扩展 `frontend/tests/unit/research/ResearchSessionPage.test.tsx`（如不存在则新建，参考同级页面测试结构）

**Interfaces:**
- Consumes: store 字段 `streaming: boolean`、`resolutionInFlight: boolean`、`turns: Turn[]`、`conflictError: string | null`、`id: string | null`
- Consumes: `useNavigate()`（已 import）
- Consumes: i18n key `research.session.complete_with_report`（新增）
- Produces: 调用 `notification.success({ ... btn: "查看报告" onClick: navigate })` 一次

### Step 1: 新增 i18n key

`frontend/src/i18n/zh-CN.ts`（行 ~2955 附近，processing 旁边）：

```typescript
processing: "处理中…",
complete_with_report: "分析已完成",
view_report: "查看报告",
```

同步加到 `frontend/src/i18n/en-US.ts`（如存在）：

```typescript
processing: "Processing...",
complete_with_report: "Analysis complete",
view_report: "View Report",
```

### Step 2: 写失败测试

`frontend/tests/unit/research/ResearchSessionPage.test.tsx`（如不存在新建）：

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { ResearchSessionPage } from "@/pages/research/ResearchSessionPage";

// mock react-router
const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
  useParams: () => ({ id: "sess-1" }),
}));

// mock store
const mockStoreState = {
  id: "sess-1",
  turns: [{ id: "t1" }],
  streaming: false,
  resolutionInFlight: false,
  conflictError: null,
  pendingCheckpoint: null,
  error: null,
  clearConflictError: vi.fn(),
};

vi.mock("@/stores/researchStore", () => ({
  useResearchStore: () => mockStoreState,
}));

// mock antd notification
const mockNotificationSuccess = vi.fn();
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    notification: { success: mockNotificationSuccess, error: vi.fn() },
  };
});

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <ConfigProvider locale={zhCN}>
    <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
  </ConfigProvider>
);

describe("ResearchSessionPage - 完成通知", () => {
  beforeEach(() => {
    mockNotificationSuccess.mockClear();
    mockNavigate.mockClear();
    mockStoreState.streaming = false;
    mockStoreState.resolutionInFlight = false;
    mockStoreState.conflictError = null;
  });

  it("streaming 从 true 变为 false 且无新 checkpoint 时弹一次完成通知", async () => {
    mockStoreState.streaming = true;
    const { rerender } = render(<ResearchSessionPage />, { wrapper });
    mockStoreState.streaming = false;
    await act(async () => { rerender(<ResearchSessionPage />); });
    expect(mockNotificationSuccess).toHaveBeenCalledTimes(1);
    // 通知含 navigate 按钮
    const call = mockNotificationSuccess.mock.calls[0][0];
    expect(call.btn.props.children).toMatch(/查看报告|View Report/);
  });

  it("streaming 仍是 true 时不弹通知", () => {
    mockStoreState.streaming = true;
    render(<ResearchSessionPage />, { wrapper });
    expect(mockNotificationSuccess).not.toHaveBeenCalled();
  });

  it("resolutionInFlight=true 时不弹通知（避免误判）", () => {
    mockStoreState.streaming = false;
    mockStoreState.resolutionInFlight = true;
    render(<ResearchSessionPage />, { wrapper });
    expect(mockNotificationSuccess).not.toHaveBeenCalled();
  });
});
```

**注意**：mock 策略如不可行（`useResearchStore` 是 selector 形式而非全量），改用 `vi.spyOn` + `useResearchStore.getState/setState` 模式。**契约保持不变**：3 个测试断言（弹一次、不在 streaming 时弹、resolutionInFlight 时不弹）。

### Step 3: 跑测试确认失败

```bash
cd frontend
npx vitest run tests/unit/research/ResearchSessionPage.test.tsx
```

期望：3 个测试全部失败（useEffect 还没加）。

### Step 4: 改 `ResearchSessionPage.tsx`

在 `import` 段（行 1-30）追加：

```tsx
import { notification } from "antd";
```

`ResearchSessionPage` 函数体内，**在已有的 conflictError useEffect 后面**（行 50-54 后）新增：

```tsx
// 完成提示：streaming false + resolutionInFlight false + 无 conflictError + 至少 1 个 turn
const notifiedRef = useRef(false);
useEffect(() => {
  if (
    !streaming &&
    !resolutionInFlight &&
    !conflictError &&
    turns.length > 0 &&
    !notifiedRef.current
  ) {
    notifiedRef.current = true;
    notification.success({
      message: t("research.session.complete_with_report"),
      description: t("research.checkpoint.processing"), // 复用"处理中…"作为描述文案，可独立换
      btn: (
        <Button
          type="primary"
          size="small"
          onClick={() => id && navigate(`/research/${id}/report`)}
        >
          {t("research.session.view_report")}
        </Button>
      ),
      duration: 6,
      placement: "topRight",
    });
  }
  // 用户重新开始时重置（id 变 / conflictError 来过）
  if (streaming || conflictError) {
    notifiedRef.current = false;
  }
}, [streaming, resolutionInFlight, conflictError, turns.length, id, navigate, t]);
```

从 `react` 引入 `useRef`：`import React, { useEffect, useRef } from "react"`。

### Step 5: 跑测试确认通过

```bash
cd frontend
npx vitest run tests/unit/research/ResearchSessionPage.test.tsx
```

期望：3 个测试全部通过。

### Step 6: TypeScript 编译

```bash
cd frontend
npx tsc --noEmit
```

期望：无报错。

### Step 7: Commit

```bash
git add frontend/src/pages/research/ResearchSessionPage.tsx \
        frontend/src/i18n/zh-CN.ts \
        frontend/src/i18n/en-US.ts \
        frontend/tests/unit/research/ResearchSessionPage.test.tsx
git commit -m "feat(research): 分析完成 antd notification 提示 + 查看报告按钮

- 新增 i18n key research.session.{complete_with_report, view_report}
- streaming=false + resolutionInFlight=false + 至少 1 turn 时弹一次
- notif 内嵌 navigate 按钮直接跳 /research/:id/report
- useRef 防重复弹，conflictError 来了重置"
```

---

## Task 3: 端到端走查（人工 + 容器）

**Files:** 无改动。

### Step 1: 前端构建

```bash
cd docker && docker compose build frontend
```

期望：构建成功（无 TS / lint 错）。

### Step 2: 重启容器

```bash
cd docker && docker compose up -d --force-recreate --no-deps frontend
```

### Step 3: 浏览器手测

操作清单（4 项）：

1. 打开 `http://localhost:5173/research/<sess-id>`，等待 checkpoint 弹出。
2. 点击"确认" — 按钮**立即变灰**，下方出现大号蓝色 banner "处理中…" + 32px Spin 旋转。
3. 再次点击"确认" — **无反应**（disabled 生效，不发请求）。
4. 后端回包 → banner 消失 → 几秒后**右上角弹出 notification**"分析已完成" + "查看报告"按钮 → 点击跳到报告页。

每一步截图存到 `/tmp/checkpoint-ux-1.png` ... `/tmp/checkpoint-ux-4.png`。

### Step 4: 不通过则回到 Task 1 / Task 2 修复

任何一步不通过 → 用浏览器 devtools 查 `resolutionInFlight` 状态 + network 面板是否有多余 409 → 按需修。

### Step 5: 跑全量单测

```bash
cd frontend
npx vitest run
```

期望：所有测试通过，无回归。

### Step 6: 写 e2e smoke 测试（如时间允许）

在 `frontend/tests/e2e/` 下加 1 个 Playwright 用例覆盖 disabled 行为（不强制本任务完成，超出 scope 可放到下个 task）。

---

## Out of Scope

- **不**改后端 / store 接口。
- **不**改 `research.checkpoint.processing` i18n key 的值（如果嫌"处理中"作为 description 重复，**新加** 1 个 `research.session.complete_hint` key 而不是覆盖）。
- **不**动 `conflictError` 处理路径（B3.5 已修过，保持原样）。
- **不**改 chat 侧按钮禁用逻辑（即使两边结构类似，本任务聚焦 research）。
- **不**做主题色适配（用硬编码 `#1677ff` / `rgba(22,119,255,0.06)` 是 antd 默认蓝，主题切换不感知，但本次只修功能性 UX bug，主题是下个 task）。
- **不**做 e2e Playwright（Task 3 Step 6 标记为可选）。

## Reused

| 工具 | 路径 | 复用方式 |
|---|---|---|
| `Spin` size="large" | antd v5 | 直接用，32px 圆环 |
| `notification.success` | antd v5 | `frontend` 已全局 import |
| `useRef` 防重复 | React 18 | 沿用 |
| `resolutionInFlight` | `researchStore.ts:86,147,284,365` | 已存在，**不**改 store |
| i18n `research.checkpoint.processing` | `i18n/zh-CN.ts:2955` | 复用 |

## Verification

```bash
cd frontend
npx vitest run tests/unit/research/CheckpointCard.test.tsx      # Task 1
npx vitest run tests/unit/research/ResearchSessionPage.test.tsx  # Task 2
npx tsc --noEmit                                                  # 两任务都过
npx vitest run                                                    # 全量回归
```

容器构建 + 浏览器手测 4 步全过（Task 3）。

## Self-Review

- **Spec coverage**：用户 4 个诉求（按钮变灰、处理中字大、动画大、完成通知）全部有 task 覆盖。
- **Placeholder scan**：所有 code block 都有完整内容，无 TBD / TODO。
- **Type consistency**：`resolutionInFlight` 在 store、CheckpointCard props、ResearchSessionPage 消费三处统一为 `boolean`，未改名。
- **Smell baseline**：Task 1 引入 banner 是单点新 UI（不算 Feature Envy）；Task 2 useEffect 内部 inline button 渲染是为了 testability（mock 简单），不抽组件，YAGNI。
