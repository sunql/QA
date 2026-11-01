/** ReportRenderer 单元测试（feat-research-entry Task 10，TDD RED→GREEN）。
 *
 * 契约（对齐 backend report_planner.py 的 JSON 形状，camelCase）：
 * - payload = { sessionId, turnId, mode, title, question, sections[], findingsRef[] }
 * - section = { id, kind, title, blocks[] }
 * - block = { type: "text"|"chart"|"table"|"bullet_list", content, sourceRefs[] }
 * - text → 段落；table → 表格；chart → 表格兜底（v1 chartType=null）；bullet_list → ul/li
 * - 块按 sections[].blocks[] 顺序渲染，每个块带 data-block-type 供顺序断言
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";

// 捕获真实传给 ECharts 的 option（JSON.stringify 会丢函数，formatter 断言只能看原对象）
const echarts = vi.hoisted(() => ({ options: [] as Record<string, unknown>[] }));

// jsdom 无法实例化 ECharts 画布 —— 与 EChart.test.tsx 同模式，占位 div 即可；
// EChart 外层包裹 div 仍带 testId（`report-chart-<chartType>`），可据其判断是否放行。
vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: React.forwardRef(function EChartsMock(
    props: { option?: Record<string, unknown>; style?: { height?: number } },
    ref: React.ForwardedRef<unknown>
  ) {
    React.useImperativeHandle(ref, () => ({ getEchartsInstance: () => ({}) }));
    if (props.option) echarts.options.push(props.option);
    return (
      <div data-testid="echarts-mock" data-height={props.style?.height}>
        {JSON.stringify(props.option)}
      </div>
    );
  }),
}));

import { ReportRenderer } from "../components/research/ReportRenderer";
import type { ReportPayload } from "../components/research/ReportRenderer";
import { i18n } from "../i18n";

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

// ---------------------------------------------------------------------------
// D3：研究线报告与聊天端完全一致（Top-N / 动态高度 / 数字格式）
// ---------------------------------------------------------------------------

/** 40 类 hbar + 后端挂的 21 类 Top-N 视图（与聊天端落库的同一份 option 形状）。 */
function makeTopNChartPayload(): ReportPayload {
  const all = Array.from({ length: 40 }, (_, i) => `供应商${String(i).padStart(2, "0")}`);
  const truncated = [...all.slice(0, 20), "其他（20 项）"];
  const base = {
    tooltip: {},
    xAxis: { type: "value" },
    series: [{ type: "bar", data: all.map((_, i) => 100 - i) }],
  };
  return makeChartPayload("hbar", {
    ...base,
    yAxis: { type: "category", data: all },
    topNOption: { ...base, yAxis: { type: "category", data: truncated } },
    topNMeta: { limit: 20, hidden: 20, total: 40 },
  });
}

function renderPayload(payload: ReportPayload) {
  return render(
    <ConfigProvider>
      <ReportRenderer payload={payload} />
    </ConfigProvider>
  );
}

function renderedCategories(): unknown[] {
  const option = JSON.parse(screen.getByTestId("echarts-mock").textContent ?? "{}") as {
    yAxis?: { data?: unknown[] };
  };
  return option.yAxis?.data ?? [];
}

describe("ReportRenderer 与聊天端一致（D3）", () => {
  beforeEach(() => {
    echarts.options.length = 0;
  });

  it("有 Top-N 视图时默认渲染截断视图（21 类，末项为「其他」）", () => {
    renderPayload(makeTopNChartPayload());

    const categories = renderedCategories();
    expect(categories).toHaveLength(21);
    expect(categories[20]).toBe("其他（20 项）");
  });

  it("「显示全部 40 项」切到全量，「只看前 20 项」再切回（与聊天端同一控件）", () => {
    renderPayload(makeTopNChartPayload());

    fireEvent.click(screen.getByText("显示全部 40 项"));
    expect(renderedCategories()).toHaveLength(40);

    fireEvent.click(screen.getByText("只看前 20 项"));
    expect(renderedCategories()).toHaveLength(21);
  });

  it("高度按当前显示的 option 推断：截断 21 类 684、全量 40 类 960", () => {
    renderPayload(makeTopNChartPayload());

    expect(screen.getByTestId("echarts-mock")).toHaveAttribute("data-height", "684");
    fireEvent.click(screen.getByText("显示全部 40 项"));
    expect(screen.getByTestId("echarts-mock")).toHaveAttribute("data-height", "960");
  });

  it("数字格式与聊天端同一条：value 轴万/亿缩写 + tooltip 千分位", () => {
    renderPayload(makeTopNChartPayload());

    const option = echarts.options[echarts.options.length - 1];
    const xAxis = option.xAxis as { axisLabel?: { formatter?: (value: unknown) => string } };
    expect(typeof xAxis.axisLabel?.formatter).toBe("function");
    expect(xAxis.axisLabel?.formatter?.(123456)).toBe("12.35万");

    const tooltip = option.tooltip as { valueFormatter?: (value: unknown) => string };
    expect(typeof tooltip.valueFormatter).toBe("function");
    expect(tooltip.valueFormatter?.(1234567)).toBe("1,234,567");
  });

  it("非中文界面下数字格式跟随 locale（与聊天端同一条，不是写死 zh）", async () => {
    await i18n.changeLanguage("en-US");
    try {
      renderPayload(makeTopNChartPayload());

      const option = echarts.options[echarts.options.length - 1];
      const xAxis = option.xAxis as { axisLabel?: { formatter?: (value: unknown) => string } };
      expect(xAxis.axisLabel?.formatter?.(123456)).toBe("123,456");
      const tooltip = option.tooltip as { valueFormatter?: (value: unknown) => string };
      expect(tooltip.valueFormatter?.(1234567)).toBe("1,234,567");
    } finally {
      await i18n.changeLanguage("zh-CN");
    }
  });

  // 反向守卫：没有 Top-N 的普通报告图不得凭空长出切换按钮。
  it("无 Top-N 视图时不渲染切换按钮", () => {
    renderPayload(
      makeChartPayload("bar", {
        xAxis: { type: "category", data: ["10月"] },
        yAxis: { type: "value" },
        series: [{ type: "bar", data: [180] }],
      })
    );

    expect(screen.queryByText(/显示全部/)).toBeNull();
    expect(screen.queryByText(/只看前/)).toBeNull();
  });
});
