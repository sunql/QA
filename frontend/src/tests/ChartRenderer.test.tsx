import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { message } from "antd";
import React from "react";

// 全局共享的 echarts 实例 mock：暴露 getDataURL，便于断言图表导出路径。
const echartsInstance = vi.hoisted(() => ({
  getDataURL: vi.fn(() => "data:image/png;base64,x"),
}));

// mock echarts-for-react：转发 ref 暴露 getEchartsInstance，避免在 jsdom 中实例化 ECharts 画布。
// 图表导出依赖 ref.current.getEchartsInstance()，普通函数 mock 无法承载。
vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: React.forwardRef(function EChartsMock(
    props: { option?: Record<string, unknown> },
    ref: React.ForwardedRef<unknown>
  ) {
    React.useImperativeHandle(ref, () => ({
      getEchartsInstance: () => echartsInstance,
    }));
    return <div data-testid="echarts-mock">{JSON.stringify(props.option)}</div>;
  }),
}));

import ChartRenderer from "../components/chat/ChartRenderer";

const createObjectUrl = vi.fn();
const revokeObjectUrl = vi.fn();
const anchorClick = vi.fn();

// jsdom 不实现 URL.createObjectURL / revokeObjectURL 的完整下载语义，且 anchor.click 不会真正导航。
// 用 defineProperty 注入 spy，与 MessageItem.test.tsx 的 navigator.clipboard 同模式。
function mockBlobDownload() {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: createObjectUrl,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: revokeObjectUrl,
  });
  Object.defineProperty(HTMLAnchorElement.prototype, "click", {
    configurable: true,
    value: anchorClick,
  });
}

// jsdom 的 Blob 未实现 .text()/.arrayBuffer()，用 FileReader 读取（真实定时器下可用）。
function readBlobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

describe("ChartRenderer", () => {
  beforeEach(() => {
    createObjectUrl.mockReset().mockReturnValue("blob:mock");
    revokeObjectUrl.mockReset();
    anchorClick.mockReset();
    echartsInstance.getDataURL.mockReset().mockReturnValue("data:image/png;base64,x");
    mockBlobDownload();
  });

  it("TABLE 类型渲染 antd 表格", () => {
    render(
      <ChartRenderer
        chartType="table"
        chartOption={{ columns: ["A", "B"] }}
        data={[
          { A: 1, B: "x" },
          { A: 2, B: "y" },
        ]}
      />
    );
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.getByText("x")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("BAR 类型渲染 echarts 组件并透传 option", () => {
    render(<ChartRenderer chartType="bar" chartOption={{ series: [{ type: "bar" }] }} />);
    const mock = screen.getByTestId("echarts-mock");
    expect(mock).toBeInTheDocument();
    expect(mock.textContent).toContain("bar");
  });

  it("无类型时渲染为空", () => {
    const { container } = render(<ChartRenderer />);
    expect(container).toBeEmptyDOMElement();
  });

  // ===== KPI 指标卡（决策 7）：不是 ECharts，走 KpiCard =====

  it("KPI 类型渲染指标卡而不是 ECharts 画布", () => {
    const { container } = render(
      <ChartRenderer
        chartType="kpi"
        chartOption={{ kpi: { label: "供应商及时交货率", value: 0.954, unit: "%", delta: null } }}
      />
    );

    expect(screen.getByText("供应商及时交货率")).toBeInTheDocument();
    // antd Statistic 把整数位与小数位拆成两个 span，整体文本要读容器
    expect(container.textContent).toContain("0.954");
    expect(screen.getByText("%")).toBeInTheDocument();
    // 没有 echarts 节点 —— KPI 走 ECharts 会渲染出一张空画布
    expect(screen.queryByTestId("echarts-mock")).toBeNull();
  });

  it("KPI 承前端主题色（服务端不发颜色）", () => {
    const { container } = render(
      <ChartRenderer
        chartType="kpi"
        chartOption={{ kpi: { label: "收货量", value: 9812, unit: null, delta: null } }}
      />
    );

    // valueStyle 落在 .ant-statistic-content 上；antd 默认把数值按千分位格式化
    expect(container.textContent).toContain("9,812");
    const content = container.querySelector<HTMLElement>(".ant-statistic-content");
    expect(content).not.toBeNull();
    // LIGHT_TOKEN.colorPrimary（默认非暗色）—— 颜色只可能来自前端 token
    expect(content?.style.color).toBe("rgb(0, 184, 169)");
  });

  it("KPI 无负载（后端降级为不发卡）时整块不渲染", () => {
    const { container } = render(<ChartRenderer chartType="kpi" chartOption={{ kpi: null }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("KPI 的 delta 为空时不渲染涨跌行（一期恒 null）", () => {
    render(
      <ChartRenderer
        chartType="kpi"
        chartOption={{ kpi: { label: "收货量", value: 9812, unit: null, delta: null } }}
      />
    );

    expect(screen.queryByText("+undefined")).toBeNull();
  });



  it("表格渲染「导出 CSV」按钮，点击导出当前展示数据", async () => {
    render(
      <ChartRenderer
        chartType="table"
        data={[
          { 物料: "A", 数量: 1 },
          { 物料: "B,2", 数量: 2 },
        ]}
      />
    );
    // 图标 SVG 带 aria-label="download"，按钮可访问名称为 "download 导出 CSV"，用子串匹配
    fireEvent.click(screen.getByRole("button", { name: /导出 CSV/ }));

    expect(createObjectUrl).toHaveBeenCalledTimes(1);
    const blob = createObjectUrl.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("text/csv;charset=utf-8");
    // BOM 前缀由 exportCsv.test.ts 的 toCsv 单测覆盖；FileReader.readAsText 解码时会剥离 BOM
    const content = await readBlobText(blob);
    expect(content).toContain("物料,数量\r\nA,1");
    expect(content).toContain('"B,2",2');

    // 触发下载的 anchor 使用固定文件名 result.csv
    // （downloadBlob 点击后即从 DOM 移除，改从 click 调用的 this 捕获实例）
    const anchor = anchorClick.mock.instances[0] as HTMLAnchorElement | undefined;
    expect(anchor).toBeDefined();
    expect(anchor?.getAttribute("download")).toBe("result.csv");
    expect(anchor?.getAttribute("href")).toBe("blob:mock");
    expect(anchorClick).toHaveBeenCalled();
  });

  it("图表渲染「导出 PNG」按钮，点击调用 getDataURL 并下载", async () => {
    render(<ChartRenderer chartType="bar" chartOption={{ series: [{ type: "bar" }] }} />);
    fireEvent.click(screen.getByRole("button", { name: /导出 PNG/ }));

    expect(echartsInstance.getDataURL).toHaveBeenCalledWith({
      type: "png",
      pixelRatio: 2,
      backgroundColor: "#fff",
    });
    expect(createObjectUrl).toHaveBeenCalledTimes(1);
    const blob = createObjectUrl.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("image/png");
    expect(await readBlobText(blob)).toBe("data:image/png;base64,x");

    const anchor = anchorClick.mock.instances[0] as HTMLAnchorElement | undefined;
    expect(anchor).toBeDefined();
    expect(anchor?.getAttribute("download")).toBe("chart.png");
    expect(anchorClick).toHaveBeenCalled();
  });

  it("无内容（无 chartType / data 为空）时不渲染导出按钮", () => {
    const { container } = render(<ChartRenderer />);
    expect(container).toBeEmptyDOMElement();

    render(<ChartRenderer chartType="table" data={null} />);
    expect(screen.queryByRole("button", { name: /导出/ })).toBeNull();
  });

  it("导出 CSV 失败时 message.error 提示", () => {
    const errorSpy = vi.spyOn(message, "error").mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    // createObjectURL 抛错以触发 catch 分支
    createObjectUrl.mockImplementation(() => {
      throw new Error("download failed");
    });
    render(
      <ChartRenderer
        chartType="table"
        data={[{ A: 1 }]}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /导出 CSV/ }));
    expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("download failed"));
    errorSpy.mockRestore();
  });

  it("导出 PNG 失败时 message.error 提示", () => {
    const errorSpy = vi.spyOn(message, "error").mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    // getDataURL 抛错
    echartsInstance.getDataURL.mockImplementation(() => {
      throw new Error("getDataURL failed");
    });
    render(<ChartRenderer chartType="bar" chartOption={{ series: [{ type: "bar" }] }} />);
    fireEvent.click(screen.getByRole("button", { name: /导出 PNG/ }));
    expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("getDataURL failed"));
    errorSpy.mockRestore();
  });

  // ===== 表格负载自取自足（多步每步不铺全量 data）=====

  it("调用方没传 data 时，表格用 chartOption 里的 rows 渲染", () => {
    render(
      <ChartRenderer
        chartType="table"
        chartOption={{
          columns: ["供应商", "数量"],
          rows: [{ 供应商: "甲", 数量: 7 }],
        }}
      />
    );

    expect(screen.getByText("甲")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("rows 为空但 columns 已声明时仍渲染表头（列取自负载而非首行推断）", () => {
    render(<ChartRenderer chartType="table" chartOption={{ columns: ["供应商"], rows: [] }} />);

    expect(screen.getByText("供应商")).toBeInTheDocument();
  });

  it("调用方传了 data 时以 data 为准（负载 rows 只作降级）", () => {
    render(
      <ChartRenderer
        chartType="table"
        chartOption={{ columns: ["A"], rows: [{ A: "来自负载" }] }}
        data={[{ A: "来自 data" }]}
      />
    );

    expect(screen.getByText("来自 data")).toBeInTheDocument();
    expect(screen.queryByText("来自负载")).toBeNull();
  });

  // ===== 截断披露（0105 落库截行）=====
  // 后端只留前 N 行并打 `truncated: true`（`chat_chart_persist`）。不告知的话，
  // 历史回放里这张表看起来就是完整结果，连「导出 CSV」导出的也是截断份 ——
  // 后端 PDF 已经如实标注，前端这一侧不能假装是全部。

  it("负载带 truncated 标记时，表格下方如实标注只显示了前几行", () => {
    render(
      <ChartRenderer
        chartType="table"
        chartOption={{
          columns: ["A"],
          rows: [{ A: 1 }, { A: 2 }],
          truncated: true,
        }}
      />
    );

    expect(screen.getByText(/表格仅显示前 2 行/)).toBeInTheDocument();
  });

  it("负载没有 truncated 标记时不出截断提示（反向守卫：不能无脑加）", () => {
    render(
      <ChartRenderer
        chartType="table"
        chartOption={{ columns: ["A"], rows: [{ A: 1 }, { A: 2 }] }}
      />
    );

    // 先证明表体真的渲染了行 —— 否则「没有提示」可能只是整块没渲染
    // （getByText("1") 会同时命中分页器的页码，所以读表体容器）
    expect(document.querySelector(".ant-table-tbody")?.textContent).toContain("1");
    expect(screen.queryByText(/表格仅显示前/)).toBeNull();
    expect(screen.queryByText(/原始结果更长/)).toBeNull();
  });

  // ===== KPI 的导出按钮（KPI 不是 ECharts，没有 PNG 可导）=====

  it("KPI 卡出「导出 CSV」按钮，不出 PNG 按钮", () => {
    render(
      <ChartRenderer
        chartType="kpi"
        chartOption={{ kpi: { label: "收货量", value: 9812, unit: null, delta: null } }}
        data={[{ label: "收货量", value: 9812 }]}
      />
    );

    expect(screen.getByRole("button", { name: /导出 CSV/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /导出 PNG/ })).toBeNull();
  });

  it("KPI 点「导出 CSV」导出的是底层数据行", async () => {
    render(
      <ChartRenderer
        chartType="kpi"
        chartOption={{ kpi: { label: "收货量", value: 9812, unit: null, delta: null } }}
        data={[{ 指标: "收货量", 数值: 9812 }]}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /导出 CSV/ }));

    expect(createObjectUrl).toHaveBeenCalledTimes(1);
    const blob = createObjectUrl.mock.calls[0][0] as Blob;
    expect(await readBlobText(blob)).toContain("指标,数值\r\n收货量,9812");
  });
});
