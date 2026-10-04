/** CheckpointCard 单元测试（feat-research-entry Task 10，TDD RED→GREEN）。
 *
 * 契约（controller 裁定）：
 * - 卡片读取 `checkpoint.prompt` 作为问题文本（不本地构造 label）
 * - confirm / modify / reject 三按钮，onAnswer(action, choice) 回调
 * - 错误按 uiHint 类分支的规则由 store 负责，本组件不落任何 code 分支
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { CheckpointCard } from "../components/research/CheckpointCard";
import type { ResearchCheckpoint } from "../types/research";

function makeCheckpoint(overrides: Partial<ResearchCheckpoint> = {}): ResearchCheckpoint {
  return {
    id: "cp-1",
    phase: "intent",
    status: "pending",
    options: {
      signal: "fixed_scope",
      resumePhase: "plan",
      arms: {
        metrics: [{ displayName: "收货量", kpiCode: "GR_QTY", confidence: 0.8 }],
        businessObjects: [],
        knowledge: [],
        conflicts: [],
      },
    },
    prompt: "三臂是否齐全？",
    userChoice: null,
    decidedAt: null,
    ...overrides,
  };
}

describe("CheckpointCard", () => {
  it("渲染 checkpoint.prompt 作为问题文本", () => {
    render(
      <ConfigProvider>
        <CheckpointCard checkpoint={makeCheckpoint()} onAnswer={() => {}} />
      </ConfigProvider>
    );
    expect(screen.getByText("三臂是否齐全？")).toBeInTheDocument();
  });

  it("渲染 options 摘要（arms.metrics 的 displayName）", () => {
    render(
      <ConfigProvider>
        <CheckpointCard checkpoint={makeCheckpoint()} onAnswer={() => {}} />
      </ConfigProvider>
    );
    expect(screen.getByText("收货量")).toBeInTheDocument();
  });

  it("confirm 按钮以空 choice 触发 onAnswer(\"confirm\")", () => {
    const onAnswer = vi.fn();
    render(
      <ConfigProvider>
        <CheckpointCard checkpoint={makeCheckpoint()} onAnswer={onAnswer} />
      </ConfigProvider>
    );
    fireEvent.click(screen.getByRole("button", { name: /确\s*认/ }));
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer).toHaveBeenCalledWith("confirm", {});
  });

  it("reject 按钮以空 choice 触发 onAnswer(\"reject\")", () => {
    const onAnswer = vi.fn();
    render(
      <ConfigProvider>
        <CheckpointCard checkpoint={makeCheckpoint()} onAnswer={onAnswer} />
      </ConfigProvider>
    );
    fireEvent.click(screen.getByRole("button", { name: /拒\s*绝/ }));
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer).toHaveBeenCalledWith("reject", {});
  });

  it("modify 展开输入框，提交后以 { question } 触发 onAnswer(\"modify\")", () => {
    const onAnswer = vi.fn();
    render(
      <ConfigProvider>
        <CheckpointCard checkpoint={makeCheckpoint()} onAnswer={onAnswer} />
      </ConfigProvider>
    );
    fireEvent.click(screen.getByRole("button", { name: /修\s*改/ }));
    const textarea = screen.getByPlaceholderText("输入修改后的要求…");
    fireEvent.change(textarea, { target: { value: "改为按月统计收货量" } });
    fireEvent.click(screen.getByRole("button", { name: "提交修改" }));
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer).toHaveBeenCalledWith("modify", { question: "改为按月统计收货量" });
  });
});
