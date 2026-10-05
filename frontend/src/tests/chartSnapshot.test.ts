import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * `chartSnapshot` 的契约测试（0105）。
 *
 * echarts 被替身化：jsdom 没有 canvas 实现，真实 `echarts.init` 拿不到 2D context
 * 会直接抛错 —— 那样测到的只是「jsdom 不支持 canvas」，不是我们的逻辑。替身让我们
 * 钉住真正属于本模块的契约：**亮色 token + 白底 + 关动画 + 用完 dispose**。
 */
const { echartsMock, chartStub } = vi.hoisted(() => {
  const chartStub = {
    setOption: vi.fn(),
    getDataURL: vi.fn(),
    dispose: vi.fn(),
  };
  const echartsMock = { init: vi.fn((..._args: unknown[]) => chartStub) };
  return { echartsMock, chartStub };
});

vi.mock("echarts", () => echartsMock);

import { LIGHT_TOKEN } from "../theme/tokens";
import { needsSnapshot, renderChartPng } from "../utils/chartSnapshot";

const PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgo=";

describe("renderChartPng", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    chartStub.getDataURL.mockReturnValue(PNG_DATA_URL);
  });

  it("返回 echarts 导出的 PNG data URL", async () => {
    const result = await renderChartPng({ series: [] });

    expect(result).toBe(PNG_DATA_URL);
  });

  it("截图参数固定为 png / 2 倍像素密度 / 白底", async () => {
    await renderChartPng({ series: [] });

    expect(chartStub.getDataURL).toHaveBeenCalledWith({
      type: "png",
      pixelRatio: 2,
      backgroundColor: "#fff",
    });
  });

  it("渲染前注入亮色主题并关掉动画（PDF 是白底静态图）", async () => {
    await renderChartPng({ series: [] });

    const option = chartStub.setOption.mock.calls[0][0];
    expect(option.animation).toBe(false);
    // 服务端只发结构、不发颜色：颜色必须在这一层补上，且补的是亮色 token
    expect(option.color).toEqual(LIGHT_TOKEN.chartPalette);
    expect(option.textStyle.color).toBe(LIGHT_TOKEN.colorText);
  });

  it("不原地改动调用方手里的 option（不可变）", async () => {
    const option = { series: [], animation: true };

    await renderChartPng(option);

    expect(option).toEqual({ series: [], animation: true });
  });

  it("渲染完 dispose 实例并移除离屏容器，不留痕迹", async () => {
    const before = document.body.childElementCount;

    await renderChartPng({ series: [] });

    expect(chartStub.dispose).toHaveBeenCalledTimes(1);
    expect(document.body.childElementCount).toBe(before);
  });

  it("离屏容器带显式宽高（ECharts 在零尺寸容器里会拿到 0×0 画布）", async () => {
    await renderChartPng({ series: [] });

    const host = echartsMock.init.mock.calls[0][0] as unknown as HTMLElement;
    expect(host.style.width).toBe("800px");
    expect(host.style.height).toBe("420px");
  });

  it("getDataURL 抛错时返回 null 并仍然 dispose", async () => {
    chartStub.getDataURL.mockImplementation(() => {
      throw new Error("canvas exploded");
    });

    const result = await renderChartPng({ series: [] });

    expect(result).toBeNull();
    expect(chartStub.dispose).toHaveBeenCalledTimes(1);
  });

  it("init 抛错时返回 null（不把导出流程带崩）", async () => {
    echartsMock.init.mockImplementationOnce(() => {
      throw new Error("no canvas");
    });

    await expect(renderChartPng({ series: [] })).resolves.toBeNull();
  });

  it("导出结果不是 PNG data URL 时返回 null（后端只收 PNG）", async () => {
    chartStub.getDataURL.mockReturnValue("data:image/svg+xml;base64,PHN2Zz4=");

    await expect(renderChartPng({ series: [] })).resolves.toBeNull();
  });

  it("失败路径同样清理离屏容器", async () => {
    const before = document.body.childElementCount;
    chartStub.getDataURL.mockImplementation(() => {
      throw new Error("boom");
    });

    await renderChartPng({ series: [] });

    expect(document.body.childElementCount).toBe(before);
  });
});

describe("needsSnapshot", () => {
  it("table / kpi 由后端原生排版，不截图", () => {
    expect(needsSnapshot("table")).toBe(false);
    expect(needsSnapshot("kpi")).toBe(false);
  });

  it("ECharts 类图表需要截图", () => {
    expect(needsSnapshot("bar")).toBe(true);
    expect(needsSnapshot("pie")).toBe(true);
    expect(needsSnapshot("heatmap")).toBe(true);
  });
});
