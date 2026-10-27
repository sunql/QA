import { describe, it, expect } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AgentRunTimeline from "../components/agent/AgentRunTimeline";
import type { AgentRunStepRead } from "../types/agentOrchestration";

/** 构造一条轨迹步骤，只需覆盖断言用到的字段。 */
function makeStep(overrides: Partial<AgentRunStepRead>): AgentRunStepRead {
  return {
    stepNo: 0,
    iterationNo: 1,
    kind: "PLAN",
    status: "SUCCEEDED",
    action: null,
    planSummary: null,
    toolName: null,
    inputJson: null,
    outputJson: null,
    answer: null,
    errorCode: null,
    errorMessage: null,
    promptTokens: 0,
    completionTokens: 0,
    cost: 0,
    startedAt: "2026-10-26T10:00:00Z",
    finishedAt: "2026-10-26T10:00:01Z",
    ...overrides,
  };
}

const planStep: AgentRunStepRead = makeStep({
  stepNo: 0,
  kind: "PLAN",
  action: "CALL_TOOL",
  planSummary: "先查询供应商风险",
  toolName: "supplier_risk",
  inputJson: { enterprise_code: "10105" },
});

const toolStep: AgentRunStepRead = makeStep({
  stepNo: 1,
  kind: "TOOL",
  toolName: "supplier_risk",
  inputJson: { enterprise_code: "10105" },
  outputJson: { text: '{"risk":"high"}', truncated: false, original_chars: 15 },
});

function renderTimeline(
  props: Partial<React.ComponentProps<typeof AgentRunTimeline>> = {},
) {
  return render(
    <ConfigProvider locale={zhCN}>
      <AgentRunTimeline
        steps={[planStep, toolStep]}
        finalAnswer={null}
        costSource="MEASURED"
        costComplete
        {...props}
      />
    </ConfigProvider>,
  );
}

describe("AgentRunTimeline", () => {
  it("渲染 PLAN 步骤的规划说明、工具名与调用参数", () => {
    renderTimeline();
    expect(screen.getByText("先查询供应商风险")).toBeInTheDocument();
    expect(screen.getAllByText("supplier_risk").length).toBeGreaterThan(0);
    expect(screen.getByText("调用工具")).toBeInTheDocument();
    expect(screen.getAllByText(/enterprise_code/).length).toBeGreaterThan(0);
  });

  it("工具输出默认折叠，点击后以 <pre> 渲染原始 JSON", () => {
    renderTimeline();
    // 默认折叠：观测文本不在 DOM 里
    expect(screen.queryByText('{"risk":"high"}')).toBeNull();
    const toggle = screen.getByRole("button", { name: /查\s*看\s*输\s*出/ });
    fireEvent.click(toggle);
    expect(screen.getByText('{"risk":"high"}')).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /隐\s*藏\s*输\s*出/ })).toBeInTheDocument();
  });

  it("outputJson.truncated=true 时显示截断标记与原始长度", () => {
    renderTimeline({
      steps: [
        makeStep({
          stepNo: 3,
          kind: "TOOL",
          toolName: "supplier_risk",
          outputJson: { text: "abc", truncated: true, original_chars: 120 },
        }),
      ],
    });
    expect(screen.getByText("输出已截断（原始长度 120 字符）")).toBeInTheDocument();
  });

  it("失败步骤渲染 errorMessage，而不是解析 outputJson.text", () => {
    renderTimeline({
      steps: [
        makeStep({
          stepNo: 2,
          kind: "TOOL",
          status: "FAILED",
          toolName: "supplier_risk",
          errorCode: "TOOL_ERROR",
          errorMessage: "工具执行异常",
          outputJson: { text: "boom", truncated: false, original_chars: 4 },
        }),
      ],
    });
    expect(screen.getByText("步骤失败：工具执行异常")).toBeInTheDocument();
    // 错误步骤的输出同样保持折叠
    expect(screen.queryByText("boom")).toBeNull();
  });

  it("渲染最终结论", () => {
    renderTimeline({ finalAnswer: "供应商 SUP000001 风险等级 high。" });
    expect(screen.getByText("供应商 SUP000001 风险等级 high。")).toBeInTheDocument();
  });

  it("成本来源 UNKNOWN 且未测量时，徽标必须显式说明「不完整」", () => {
    renderTimeline({ costSource: "UNKNOWN", costComplete: false });
    expect(screen.getByText("成本未测量（不完整）")).toBeInTheDocument();
  });

  it("成本已测量时徽标显示已测量", () => {
    renderTimeline({ costSource: "MEASURED", costComplete: true });
    expect(screen.getByText("成本已测量")).toBeInTheDocument();
  });

  it("JSON 以转义文本渲染，绝不使用 dangerouslySetInnerHTML", () => {
    const { container } = renderTimeline({
      steps: [
        makeStep({
          stepNo: 4,
          kind: "TOOL",
          toolName: "supplier_risk",
          inputJson: { query: "<script>alert('x')</script>" },
        }),
      ],
    });
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert('x')</script>");
  });

  it("没有步骤时展示空状态", () => {
    renderTimeline({ steps: [] });
    expect(screen.getByText("暂无执行轨迹")).toBeInTheDocument();
  });
});
