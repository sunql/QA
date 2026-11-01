/** L4 执行轨迹卡（事后快照）。
 *
 * 契约：
 * - 默认折叠，只显示一行摘要（步数 / 轮次 / 总耗时）；
 * - 展开后按轮次分组列出每一步的工具名、耗时、参数摘要与结果摘要；
 * - 失败步显示错误摘要；未知工具名原样展示，不崩。
 *
 * 注意 antd `Collapse` 默认**不挂载**面板内容：不展开就查不到步骤（不是组件坏了）。
 */
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import AgentTraceCard from "../components/chat/AgentTraceCard";
import type { AgentTrace, AgentTraceStep } from "../types/chat";

const PANEL_LABEL = "查看执行轨迹";

function makeStep(overrides: Partial<AgentTraceStep> = {}): AgentTraceStep {
  return {
    index: 0,
    iteration: 0,
    tool: "list_tables",
    status: "ok",
    elapsedMs: 12,
    args: { keyword: "supply" },
    resultSummary: { tables: 3 },
    ...overrides,
  };
}

function makeTrace(overrides: Partial<AgentTrace> = {}): AgentTrace {
  return {
    kind: "l4_agent_trace",
    status: "answered",
    iterations: 2,
    totalMs: 1234,
    stepCount: 1,
    steps: [makeStep()],
    ...overrides,
  };
}

describe("AgentTraceCard", () => {
  it("renders a one-line summary and stays collapsed by default", () => {
    render(<AgentTraceCard trace={makeTrace()} />);

    expect(screen.getByText(new RegExp(PANEL_LABEL))).toBeTruthy();
    expect(screen.getByText(/1 步/)).toBeTruthy();
    expect(screen.getByText(/2 轮/)).toBeTruthy();
    // 折叠时面板内容未挂载：步骤工具名不应出现在 DOM 里
    expect(screen.queryByText("list_tables")).toBeNull();
  });

  it("expands to show steps grouped by iteration", () => {
    const trace = makeTrace({
      stepCount: 3,
      steps: [
        makeStep({ index: 0, iteration: 0 }),
        makeStep({ index: 1, iteration: 0, tool: "describe_table", args: { table: "DTL_SUPPLY" } }),
        makeStep({ index: 2, iteration: 1, tool: "execute_sql", args: { sqlPreview: "SELECT 1" } }),
      ],
    });

    render(<AgentTraceCard trace={trace} />);
    fireEvent.click(screen.getByText(new RegExp(PANEL_LABEL)));

    expect(screen.getByText("list_tables")).toBeTruthy();
    expect(screen.getByText("describe_table")).toBeTruthy();
    expect(screen.getByText("execute_sql")).toBeTruthy();
    expect(screen.getByText(/第 1 轮/)).toBeTruthy();
    expect(screen.getByText(/第 2 轮/)).toBeTruthy();
    expect(screen.getByText(/SELECT 1/)).toBeTruthy();
  });

  it("shows the error summary on failed steps", () => {
    const trace = makeTrace({
      steps: [
        makeStep({
          status: "error",
          tool: "execute_sql",
          error: "sql_syntax: near SELECT",
          args: {},
          resultSummary: {},
        }),
      ],
    });

    render(<AgentTraceCard trace={trace} />);
    fireEvent.click(screen.getByText(new RegExp(PANEL_LABEL)));

    expect(screen.getByText(/sql_syntax: near SELECT/)).toBeTruthy();
  });

  it("flags a trace that hit the iteration limit", () => {
    render(<AgentTraceCard trace={makeTrace({ status: "max_iterations" })} />);

    expect(screen.getByText(/达到轮次上限/)).toBeTruthy();
  });

  it("renders unknown tool names verbatim", () => {
    const trace = makeTrace({
      steps: [makeStep({ tool: "brand_new_tool", args: {}, resultSummary: {} })],
    });

    render(<AgentTraceCard trace={trace} />);
    fireEvent.click(screen.getByText(new RegExp(PANEL_LABEL)));

    expect(screen.getByText("brand_new_tool")).toBeTruthy();
  });
});
