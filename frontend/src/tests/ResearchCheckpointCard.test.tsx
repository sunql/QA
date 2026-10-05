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

describe("CheckpointCard 结构化渲染（W1）", () => {
  const conflictOptions = {
    signal: "metric_ambiguous",
    resumePhase: "plan",
    arms: { metrics: [], businessObjects: [], knowledge: [], conflicts: [] },
    conflicts: [
      {
        kind: "metric_ambiguous",
        detail: "前两名 metric 分差 < 0.05",
        candidates: [
          { kpiCode: "KPI-A", displayName: "供货量", confidence: 0.71 },
          { kpiCode: "KPI-B", displayName: "收货量", confidence: 0.66 },
        ],
      },
    ],
  };

  it("目标行按 phase 显示「本次针对什么」", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({ phase: "runtime_dynamic", options: conflictOptions })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("本次针对")).toBeInTheDocument();
    expect(screen.getByText("确认语义歧义的处理方式")).toBeInTheDocument();
  });

  it("runtime_dynamic：渲染歧义种类、detail 与候选（名称 + 置信度）", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({ phase: "runtime_dynamic", options: conflictOptions })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("指标歧义")).toBeInTheDocument();
    expect(screen.getByText("前两名 metric 分差 < 0.05")).toBeInTheDocument();
    expect(screen.getByText("供货量")).toBeInTheDocument();
    expect(screen.getByText(/0\.71/)).toBeInTheDocument();
  });

  it("runtime_dynamic 但 conflicts 为空：给空态文案而非空白", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({
            phase: "runtime_dynamic",
            options: { ...conflictOptions, conflicts: [] },
          })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(
      screen.getByText("本次未返回歧义明细，可直接点「修改」补充说明。")
    ).toBeInTheDocument();
  });

  it("planning：双键回落读取 steps（snake_case 与 camelCase 都能取到）", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({
            phase: "planning",
            options: {
              arms: { metrics: [], conflicts: [] },
              plan: { steps: [{ sub_question: "按收货地点拆分" }, { subQuestion: "按月拆分" }] },
            },
          })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("按收货地点拆分")).toBeInTheDocument();
    expect(screen.getByText("按月拆分")).toBeInTheDocument();
  });

  it("hypothesis：候选渲染为可勾选项，confirm 提交选中的下标", () => {
    const onAnswer = vi.fn();
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({
            phase: "hypothesis",
            options: {
              arms: { metrics: [], conflicts: [] },
              candidates: [
                { statement: "供货量下降因供应商切换", driver: "GR_QTY" },
                { statement: "供货量下降因收货地点变化", driver: "RCV_SITE" },
              ],
            },
          })}
          onAnswer={onAnswer}
        />
      </ConfigProvider>
    );
    // 下标顺序 = candidates 数组顺序：点第 2 项 ⇒ 提交 [1]
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: /确\s*认/ }));
    expect(onAnswer).toHaveBeenCalledWith("confirm", { selectedIndexes: [1] });
  });

  it("hypothesis 但候选为空：给空态文案而非空白", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({
            phase: "hypothesis",
            options: { arms: { metrics: [], conflicts: [] }, candidates: [] },
          })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(
      screen.getByText(
        "本轮未生成候选假设（模型不可用或解析失败），可直接点「修改」补充研究方向。"
      )
    ).toBeInTheDocument();
  });
});

describe("CheckpointCard 候选勾选提示（默认验证全部）", () => {
  const candidateOptions = {
    arms: { metrics: [], conflicts: [] },
    candidates: [
      { statement: "供货量下降因供应商切换", driver: "GR_QTY" },
      { statement: "供货量下降因收货地点变化", driver: "RCV_SITE" },
    ],
  };

  it("hypothesis：候选块渲染「默认验证全部」提示", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({ phase: "hypothesis", options: candidateOptions })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("默认验证全部，勾选可缩小范围")).toBeInTheDocument();
  });

  it.each(["intent", "planning", "runtime_dynamic"] as const)(
    "非 hypothesis 相位（%s）：不渲染该提示",
    (phase) => {
      render(
        <ConfigProvider>
          <CheckpointCard
            checkpoint={makeCheckpoint({ phase, options: candidateOptions })}
            onAnswer={() => {}}
          />
        </ConfigProvider>
      );
      expect(screen.queryByText("默认验证全部，勾选可缩小范围")).not.toBeInTheDocument();
    }
  );
});
