import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ChatMessageRead } from "../types/chatHistory";

const { api, snapshot } = vi.hoisted(() => ({
  api: { loadSessionMessages: vi.fn() },
  snapshot: { renderChartPng: vi.fn() },
}));

vi.mock("../api/chatHistory", () => ({
  loadSessionMessages: (...args: unknown[]) => api.loadSessionMessages(...args),
}));

// 只替身真实渲染（jsdom 无 canvas），needsSnapshot 保持真身 —— 它是本模块的判据
vi.mock("../utils/chartSnapshot", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../utils/chartSnapshot")>();
  return {
    ...actual,
    renderChartPng: (...args: unknown[]) => snapshot.renderChartPng(...args),
  };
});

import { collectExportCharts } from "../utils/collectExportCharts";
import { chartHeightOf } from "../utils/chartLayout";

const PNG = "data:image/png;base64,AA==";

/** 40 类目 ⇒ chartHeightOf 封顶 960（真实高度推断，不 mock）。 */
const TALL_OPTION = {
  yAxis: { type: "category", data: Array.from({ length: 40 }, (_, i) => `C${i}`) },
  series: [{ type: "bar", data: [] }],
};


function message(overrides: Partial<ChatMessageRead> & { id: number }): ChatMessageRead {
  return {
    role: "assistant",
    content: "答",
    question: null,
    sql: null,
    createdTime: "2026-01-01T10:00:00Z",
    interrupted: false,
    ...overrides,
  };
}

function withResponse(messages: ChatMessageRead[]): void {
  api.loadSessionMessages.mockResolvedValue({ sessionId: "s-1", messages });
}

describe("collectExportCharts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    snapshot.renderChartPng.mockResolvedValue(PNG);
    withResponse([]);
  });

  it("返回 messageId + PNG，供后端按轮次挂图", async () => {
    withResponse([message({ id: 7, chartType: "bar", chartOption: { series: [] } })]);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([{ messageId: 7, imagePng: PNG }]);
  });

  it("把落库的 chartOption 交给渲染层（图与历史回放一致，而不是屏幕上的现值）", async () => {
    const option = { series: [{ type: "line", data: [1, 2] }] };
    withResponse([message({ id: 7, chartType: "line", chartOption: option })]);

    await collectExportCharts("s-1");

    expect(snapshot.renderChartPng).toHaveBeenCalledWith(option);
  });

  it("跳过 user 行与 table/kpi（后两者由后端原生排版，比位图清晰）", async () => {
    withResponse([
      message({ id: 1, role: "user" as const, chartType: "bar", chartOption: {} }),
      message({ id: 2, chartType: "table", chartOption: { columns: [], rows: [] } }),
      message({ id: 3, chartType: "kpi", chartOption: { kpi: { label: "x" } } }),
    ]);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([]);
    expect(snapshot.renderChartPng).not.toHaveBeenCalled();
  });

  it("跳过白名单不认识的 chartType（可能来自更早版本的后端）", async () => {
    withResponse([message({ id: 1, chartType: "radar-gl", chartOption: { series: [] } })]);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([]);
    expect(snapshot.renderChartPng).not.toHaveBeenCalled();
  });

  it("跳过没有 chartOption 的轮次（无结构可渲染）", async () => {
    withResponse([message({ id: 1, chartType: "bar", chartOption: null })]);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([]);
    expect(snapshot.renderChartPng).not.toHaveBeenCalled();
  });

  it("单张渲染失败只丢那一张，其余照带", async () => {
    withResponse([
      message({ id: 1, chartType: "bar", chartOption: { series: [] } }),
      message({ id: 2, chartType: "pie", chartOption: { series: [] } }),
    ]);
    snapshot.renderChartPng.mockResolvedValueOnce(null).mockResolvedValueOnce(PNG);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([{ messageId: 2, imagePng: PNG }]);
  });

  it("传入 messageId 时只处理该条（与单条问答导出对齐）", async () => {
    withResponse([
      message({ id: 1, chartType: "bar", chartOption: { series: [] } }),
      message({ id: 2, chartType: "pie", chartOption: { series: [] } }),
    ]);

    const charts = await collectExportCharts("s-1", 2);

    expect(charts).toEqual([{ messageId: 2, imagePng: PNG }]);
  });

  it("导出按像素预算截断：默认高度图约 35 张封顶，而不是让后端 422 把整份导出打回", async () => {
    withResponse(
      Array.from({ length: 80 }, (_, index) =>
        message({ id: index + 1, chartType: "bar", chartOption: { series: [] } })
      )
    );

    const charts = await collectExportCharts("s-1");

    // 默认 320 高：1600 × 2 × 320 = 1.024M px；36M / 1.024M = 35.15 ⇒ 35 张
    expect(charts).toHaveLength(35);
  });

  it("高图按像素（而不是张数）截断：960 高只留最近 11 张，并只告警一次", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(chartHeightOf(TALL_OPTION)).toBe(960);
    withResponse(
      Array.from({ length: 20 }, (_, index) =>
        message({ id: index + 1, chartType: "bar", chartOption: TALL_OPTION })
      )
    );

    const charts = await collectExportCharts("s-1");

    // 36M / 3.072M = 11.7 ⇒ 11 张；丢最早 1..9，保留最新的 10..20
    expect(charts).toHaveLength(11);
    expect(charts[0].messageId).toBe(10);
    expect(charts[charts.length - 1].messageId).toBe(20);
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });

  it("拿不到消息流时整份不带图，导出照常（图是增强不是主功能）", async () => {
    api.loadSessionMessages.mockRejectedValue(new Error("boom"));

    await expect(collectExportCharts("s-1")).resolves.toEqual([]);
  });

  it("拿不到消息流时留下可 grep 的痕迹（否则「PDF 永远没图」只能靠猜）", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    api.loadSessionMessages.mockRejectedValue(new Error("boom"));

    await collectExportCharts("s-1");

    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it("按消息流上限读取**最新**一批，对齐导出的最后 500 轮窗口", async () => {
    await collectExportCharts("s-1");

    expect(api.loadSessionMessages).toHaveBeenCalledWith("s-1", 1000, { tail: true });
  });

  it("渲染并发有上限，不会一次建出全部离屏画布", async () => {
    // 每块同时建 4 张离屏画布（800 CSS × 动态高度，最高 960），不一次建出全部
    let inFlight = 0;
    let peak = 0;
    snapshot.renderChartPng.mockImplementation(async () => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await Promise.resolve();
      inFlight -= 1;
      return PNG;
    });
    withResponse(
      Array.from({ length: 20 }, (_, index) =>
        message({ id: index + 1, chartType: "bar", chartOption: { series: [] } })
      )
    );

    const charts = await collectExportCharts("s-1");

    expect(charts).toHaveLength(20);
    expect(peak).toBeLessThanOrEqual(4);
  });
});
