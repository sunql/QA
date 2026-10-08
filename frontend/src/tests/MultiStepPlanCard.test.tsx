/** 多步每步出图（决策 3）：每个 step 挂同一个 ChartRenderer。
 *
 * 契约：**图跟着步骤走** —— 第 0 步的图不能画在第 1 步下面（所以断言按「有几张图、
 * 各自的数据分别是谁」来钉，而不是笼统地说「有图」）。
 *
 * 注意 antd `Collapse` 默认**不挂载**面板内容：不展开就什么都查不到（不是组件坏了）。
 */
import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";

vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: React.forwardRef(function EChartsMock(
    props: { option?: Record<string, unknown> },
    ref: React.ForwardedRef<unknown>
  ) {
    React.useImperativeHandle(ref, () => ({
      getEchartsInstance: () => ({ getDataURL: () => "data:image/png;base64,x" }),
    }));
    return <div data-testid="echarts-mock">{JSON.stringify(props.option)}</div>;
  }),
}));

import MultiStepPlanCard from "../components/chat/MultiStepPlanCard";
import type { MultiStepStep } from "../types/chat";

const PANEL_LABEL = "查看分析计划";

function renderExpanded(
  steps: MultiStepStep[],
  onResume?: (runId: string, fromStepIndex: number) => void,
  disabled?: boolean
) {
  render(<MultiStepPlanCard steps={steps} onResume={onResume} disabled={disabled} />);
  fireEvent.click(screen.getByText(PANEL_LABEL));
}

function makeStep(overrides: Partial<MultiStepStep> = {}): MultiStepStep {
  return {
    stepIndex: 0,
    description: "各供应商收货量",
    subQuestion: "各供应商的收货量是多少",
    aggregationOnly: false,
    status: "done",
    sql: "SELECT 1",
    summary: null,
    error: null,
    ...overrides,
  };
}

describe("MultiStepPlanCard 每步出图", () => {
  it("带图的步骤渲染出自己的图表", () => {
    renderExpanded([
      makeStep({ chartType: "hbar", chartOption: { series: [{ type: "bar", data: [9812] }] } }),
    ]);

    const chart = screen.getByTestId("echarts-mock");
    expect(chart.textContent).toContain('"type":"bar"');
    expect(chart.textContent).toContain("9812");
  });

  it("没有图的步骤不渲染图表（失败步骤后端不发图表字段）", () => {
    renderExpanded([makeStep({ stepIndex: 1, status: "error", error: "ORA-00942", chartType: null })]);

    expect(screen.queryByTestId("echarts-mock")).toBeNull();
  });

  it("KPI 步骤渲染指标卡而非 echarts 画布", () => {
    renderExpanded([
      makeStep({
        chartType: "kpi",
        chartOption: { kpi: { label: "总收货量", value: 9812, unit: "件", delta: null } },
      }),
    ]);

    expect(screen.getByText("总收货量")).toBeInTheDocument();
    expect(screen.getByText("件")).toBeInTheDocument();
    expect(screen.queryByTestId("echarts-mock")).toBeNull();
  });

  it("一步有图一步无图时只有一张图（不串到隔壁步骤）", () => {
    renderExpanded([
      makeStep({ stepIndex: 0, chartType: "line", chartOption: { series: [{ type: "line", data: [1, 2] }] } }),
      makeStep({ stepIndex: 1, chartType: null, chartOption: null }),
    ]);

    expect(screen.getAllByTestId("echarts-mock")).toHaveLength(1);
    expect(screen.getByTestId("echarts-mock").textContent).toContain('"type":"line"');
  });

  it("每步各出自己的图（两张图各自的数据都在，且没有错位）", () => {
    renderExpanded([
      makeStep({ stepIndex: 0, chartType: "bar", chartOption: { series: [{ type: "bar", data: [111] }] } }),
      makeStep({ stepIndex: 1, chartType: "line", chartOption: { series: [{ type: "line", data: [222] }] } }),
    ]);

    const charts = screen.getAllByTestId("echarts-mock");
    expect(charts).toHaveLength(2);
    expect(charts[0].textContent).toContain("111");
    expect(charts[1].textContent).toContain("222");
  });

  it("步骤的图渲染自己的 rationale 说明行与折叠数据表（Task 8）", () => {
    renderExpanded([
      makeStep({
        chartType: "hbar",
        chartOption: { series: [{ type: "bar", data: [9812] }] },
        tableOption: { columns: ["供应商"], rows: [{ 供应商: "甲" }] },
        visualRationale: { code: "R11_HBAR_MANY_ROWS", params: { rows: 5 } },
      }),
    ]);

    expect(screen.getByText("类目较多（5 项），以横向柱状图呈现，附数据表")).toBeInTheDocument();
    expect(screen.getByText("数据表")).toBeInTheDocument();
  });
});

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

  it("汇总步即使失败也不渲染续跑按钮（其步号恒越界，后端必拒）", () => {
    // 汇总步不落 multi_step_step 行，但它的 stepIndex = len(data_steps)，
    // 恰好等于 steps 行数 ⇒ 续跑请求必然落在 `start >= len(steps)` 的范围闸上，
    // 用户点下去只会拿到 409。故前端根本不渲染这个按钮（后端越界闸见
    // test_multi_step_resume_api.py::testResumeOutOfRangeFromStepIndexRejected）。
    // 反向自检：删掉 MultiStepPlanCard 里的 `!s.aggregationOnly` ⇒ 本用例红。
    const onResume = vi.fn();
    renderExpanded(
      [
        makeStep({ stepIndex: 1, status: "error", error: "boom", runId: "r-1", aggregationOnly: true }),
      ],
      onResume
    );

    expect(screen.queryByTestId("resume-run")).toBeNull();
  });

  it("普通失败步与汇总步并存时，只有普通步拿到续跑按钮", () => {
    // 挡「把整个按钮删掉」这类过宽修法：普通步必须仍有入口。
    const onResume = vi.fn();
    renderExpanded(
      [
        makeStep({ stepIndex: 0, status: "error", error: "boom", runId: "r-1" }),
        makeStep({
          stepIndex: 1,
          status: "error",
          error: "总结失败",
          runId: "r-1",
          aggregationOnly: true,
        }),
      ],
      onResume
    );

    const buttons = screen.getAllByTestId("resume-run");
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(onResume).toHaveBeenCalledWith("r-1", 0);
  });

  it("压缩步骤显示压缩徽章与行数", () => {
    renderExpanded([
      makeStep({ status: "compressed", originalRows: 1000, compressedRows: 30 }),
    ]);

    expect(screen.getByText("已压缩")).toBeInTheDocument();
    expect(screen.getByText("数据已压缩（1000 → 30 行）")).toBeInTheDocument();
  });

  it("流式进行中（disabled）时续跑按钮禁用，防重复点击", () => {
    renderExpanded(
      [makeStep({ stepIndex: 1, status: "error", error: "boom", runId: "r-1" })],
      vi.fn(),
      true
    );

    expect(screen.getByTestId("resume-run")).toBeDisabled();
  });
});
