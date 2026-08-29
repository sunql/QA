import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
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
});
