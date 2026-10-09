/** ReportRenderer 单元测试（feat-research-entry Task 10，TDD RED→GREEN）。
 *
 * 契约（对齐 backend report_planner.py 的 JSON 形状，camelCase）：
 * - payload = { sessionId, turnId, mode, title, question, sections[], findingsRef[] }
 * - section = { id, kind, title, blocks[] }
 * - block = { type: "text"|"chart"|"table"|"bullet_list", content, sourceRefs[] }
 * - text → 段落；table → 表格；chart → 表格兜底（v1 chartType=null）；bullet_list → ul/li
 * - 块按 sections[].blocks[] 顺序渲染，每个块带 data-block-type 供顺序断言
 */
import { describe, expect, it, vi } from "vitest";
import React from "react";
import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";

// jsdom 无法实例化 ECharts 画布 —— 与 EChart.test.tsx 同模式，占位 div 即可；
// EChart 外层包裹 div 仍带 testId（`report-chart-<chartType>`），可据其判断是否放行。
vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: React.forwardRef(function EChartsMock(
    props: { option?: Record<string, unknown> },
    ref: React.ForwardedRef<unknown>
  ) {
    React.useImperativeHandle(ref, () => ({ getEchartsInstance: () => ({}) }));
    return <div data-testid="echarts-mock">{JSON.stringify(props.option)}</div>;
  }),
}));

import { ReportRenderer } from "../components/research/ReportRenderer";
import type { ReportPayload } from "../components/research/ReportRenderer";

function makePayload(): ReportPayload {
  return {
    sessionId: "s-1",
    turnId: "t-1",
    mode: "research",
    title: "本月收货量分析报告",
    question: "本月收货量趋势如何？",
    sections: [
      {
        id: "summary",
        kind: "summary",
        title: "执行摘要",
        blocks: [
          { type: "text", content: "收货量整体环比上升。", sourceRefs: [] },
          { type: "bullet_list", content: ["指标 A 上升", "指标 B 持平"], sourceRefs: [] },
          {
            type: "table",
            content: {
              columns: ["月份", "收货量"],
              rows: [{ 月份: "8月", 收货量: 120 }, { 月份: "9月", 收货量: 150 }],
            },
            sourceRefs: [],
          },
        ],
      },
      {
        id: "detail",
        kind: "detail",
        title: "明细",
        blocks: [
          {
            type: "chart",
            content: {
              title: "收货量月度趋势",
              chartType: null,
              columns: ["月份", "收货量"],
              rows: [{ 月份: "10月", 收货量: 180 }],
            },
            sourceRefs: [],
          },
        ],
      },
    ],
    findingsRef: [{ findingId: "f-1", claim: "收货量上升", confidence: 0.9, verified: true }],
  };
}

/** R3-B：单个 chart 块的 payload —— chartType + 预渲染 echartsOption 驱动渲染门。 */
function makeChartPayload(
  chartType: string | null,
  echartsOption: Record<string, unknown> | null
): ReportPayload {
  return {
    sessionId: "s-1",
    turnId: "t-1",
    mode: "research",
    title: "报告",
    question: "问题？",
    sections: [
      {
        id: "detail",
        kind: "detail",
        title: "明细",
        blocks: [
          {
            type: "chart",
            content: {
              title: "收货量月度趋势",
              chartType,
              columns: ["月份", "收货量"],
              data: [{ 月份: "10月", 收货量: 180 }],
              echartsOption,
            },
            sourceRefs: [],
          },
        ],
      },
    ],
  };
}

describe("ReportRenderer", () => {
  it("渲染报告标题与原始问题", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    expect(screen.getByText("本月收货量分析报告")).toBeInTheDocument();
    expect(screen.getByText("本月收货量趋势如何？")).toBeInTheDocument();
  });

  it("渲染 section 标题", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    expect(screen.getByText("执行摘要")).toBeInTheDocument();
    expect(screen.getByText("明细")).toBeInTheDocument();
  });

  it("text 块渲染为段落", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    expect(screen.getByText("收货量整体环比上升。")).toBeInTheDocument();
  });

  it("bullet_list 块渲染为无序列表", () => {
    const { container } = render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    const items = container.querySelectorAll("li");
    const texts = Array.from(items).map((li) => li.textContent);
    expect(texts).toContain("指标 A 上升");
    expect(texts).toContain("指标 B 持平");
  });

  it("table 块渲染表头与数据行", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    expect(screen.getByText("8月")).toBeInTheDocument();
    expect(screen.getByText("150")).toBeInTheDocument();
  });

  it("chart 块以表格兜底渲染（v1 chartType=null）", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    // chart 块的列头（月份）在表格兜底中渲染
    expect(screen.getByText("收货量月度趋势")).toBeInTheDocument();
    const header = screen.getAllByText("月份");
    expect(header.length).toBeGreaterThanOrEqual(2); // table + chart 兜底各一列
  });

  it("按 sections[].blocks[] 顺序渲染，data-block-type 序列正确", () => {
    const { container } = render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    const types = Array.from(container.querySelectorAll("[data-testid='report-block']")).map(
      (el) => el.getAttribute("data-block-type"),
    );
    expect(types).toEqual(["text", "bullet_list", "table", "chart"]);
  });

  // R3-B：chartType=kpi 是单指标展示，不是 ECharts 结构 —— 即便 echartsOption 非空
  // 也必须走表格兜底，否则喂给 ECharts 会渲染成空白图。
  it("chartType=kpi 即使 echartsOption 非空也回退表格（不喂 ECharts）", () => {
    render(
      <ConfigProvider>
        <ReportRenderer
          payload={makeChartPayload("kpi", {
            series: [{ type: "kpi", data: [180] }],
          })}
        />
      </ConfigProvider>
    );
    expect(screen.queryByTestId("report-chart-kpi")).toBeNull();
    // 表格兜底渲染列头与数据
    expect(screen.getByText("收货量月度趋势")).toBeInTheDocument();
    expect(screen.getByText("10月")).toBeInTheDocument();
  });

  it("chartType=bar 且有 echartsOption 时渲染 ECharts 元素", () => {
    render(
      <ConfigProvider>
        <ReportRenderer
          payload={makeChartPayload("bar", {
            xAxis: { type: "category", data: ["10月"] },
            yAxis: { type: "value" },
            series: [{ type: "bar", data: [180] }],
          })}
        />
      </ConfigProvider>
    );
    expect(screen.getByTestId("report-chart-bar")).toBeInTheDocument();
  });
});
