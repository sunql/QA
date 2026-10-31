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

import {
  collectExportCharts,
  MAX_EXPORT_IMAGE_BYTES,
  MAX_EXPORT_TOTAL_IMAGE_BYTES,
} from "../utils/collectExportCharts";
import { chartHeightOf } from "../utils/chartLayout";

const PNG_PREFIX = "data:image/png;base64,";
const PNG = `${PNG_PREFIX}AA==`;

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

  it("有 Top-N 视图时截图用截断视图（与屏幕上默认看到的一致）", async () => {
    const truncated = {
      yAxis: { type: "category", data: Array.from({ length: 21 }, (_, i) => `C${i}`) },
      series: [{ type: "bar", data: [] }],
    };
    const option = {
      ...TALL_OPTION,
      topNOption: truncated,
      topNMeta: { limit: 20, hidden: 20, total: 40 },
    };
    withResponse([message({ id: 7, chartType: "hbar", chartOption: option })]);

    await collectExportCharts("s-1");

    expect(snapshot.renderChartPng).toHaveBeenCalledWith(truncated);
  });

  it("像素预算按当前显示的（截断）视图算：40 类全量 960 高会少带图", async () => {
    const truncated = {
      yAxis: { type: "category", data: Array.from({ length: 21 }, (_, i) => `C${i}`) },
      series: [{ type: "bar", data: [] }],
    };
    const option = {
      ...TALL_OPTION,
      topNOption: truncated,
      topNMeta: { limit: 20, hidden: 20, total: 40 },
    };
    withResponse(
      Array.from({ length: 20 }, (_, index) =>
        message({ id: index + 1, chartType: "hbar", chartOption: option })
      )
    );

    const charts = await collectExportCharts("s-1");

    // 截断视图 684 高 ⇒ 3,200 × 684 = 2.1888M/张 ⇒ 36M / 2.1888M = 16.4 ⇒ 16 张。
    // 若错用全量 960 高则只剩 11 张 —— 这个差值就是断言本身。
    expect(charts).toHaveLength(16);
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

  it("单张位图超过后端单张上限时只丢那一张，而不是让整份导出 422", async () => {
    withResponse([
      message({ id: 1, chartType: "bar", chartOption: { series: [] } }),
      message({ id: 2, chartType: "pie", chartOption: { series: [] } }),
    ]);
    const oversize = `${PNG_PREFIX}${"A".repeat(MAX_EXPORT_IMAGE_BYTES * 4 / 3 + 8)}`;
    snapshot.renderChartPng.mockResolvedValueOnce(oversize).mockResolvedValueOnce(PNG);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([{ messageId: 2, imagePng: PNG }]);
  });

  it("刚好卡在上限内的大图照常带上（预检不能把正常大图也丢掉）", async () => {
    withResponse([message({ id: 1, chartType: "bar", chartOption: { series: [] } })]);
    const nearLimit = `${PNG_PREFIX}${"A".repeat(Math.floor(MAX_EXPORT_IMAGE_BYTES * 4 / 3) - 4)}`;
    snapshot.renderChartPng.mockResolvedValueOnce(nearLimit);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([{ messageId: 1, imagePng: nearLimit }]);
  });

  it("单张渲染抛异常也只丢那一张（内部异常不能拖垮整份导出）", async () => {
    withResponse([
      message({ id: 1, chartType: "bar", chartOption: { series: [] } }),
      message({ id: 2, chartType: "pie", chartOption: { series: [] } }),
    ]);
    snapshot.renderChartPng
      .mockRejectedValueOnce(new Error("dispose boom"))
      .mockResolvedValueOnce(PNG);

    await expect(collectExportCharts("s-1")).resolves.toEqual([
      { messageId: 2, imagePng: PNG },
    ]);
  });

  it("总解码字节超预算时从最早的丢起（不让后端 20 MiB 那道闸 422 整份导出）", async () => {
    withResponse(
      Array.from({ length: 12 }, (_, index) =>
        message({ id: index + 1, chartType: "bar", chartOption: { series: [] } })
      )
    );
    // 每张 ≈ 1/12 总预算 +1 字节（单张仍低于 2 MiB 上限）⇒ 12 张合计越界，只装得下 11 张
    const perImageDecoded = Math.floor(MAX_EXPORT_TOTAL_IMAGE_BYTES / 12) + 1;
    const big = `${PNG_PREFIX}${"A".repeat(Math.ceil((perImageDecoded * 4) / 3))}`;
    snapshot.renderChartPng.mockResolvedValue(big);

    const charts = await collectExportCharts("s-1");

    expect(charts.map((chart) => chart.messageId)).toEqual([
      2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
    ]);
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

// ---------------------------------------------------------------------------
// 分节插图（2026-10-31，D1）：多步报告的图要落在它那一节的标题下，因此位图按
// `(messageId, stepIndex)` 逐块归属；该轮的**顶层图不再截**（分节图才是正文里的图）。
// ---------------------------------------------------------------------------

const STEP_OPTION_0 = { marker: "step-0" };
const STEP_OPTION_2 = { marker: "step-2" };

/** 一条带分节块的多步消息：顶层图故意给可区分的 option，用于证明它没被截。 */
function multiStepMessage(
  overrides: Partial<ChatMessageRead> = {}
): ChatMessageRead {
  return message({
    id: 7,
    chartType: "bar",
    chartOption: { marker: "top-level" },
    stepsSnapshot: [
      { stepIndex: 0, chartType: "hbar", chartOption: STEP_OPTION_0 },
      { stepIndex: 1, chartType: "kpi", chartOption: { kpi: { label: "总量" } } },
      { stepIndex: 2, chartType: "bar", chartOption: STEP_OPTION_2 },
    ],
    reportSections: [
      { kind: "text", content: "导语" },
      { kind: "chart", stepIndex: 0, title: "步骤 1" },
      { kind: "chart", stepIndex: 1, title: "步骤 2" },
      { kind: "chart", stepIndex: 2, title: "步骤 3" },
    ],
    ...overrides,
  });
}

describe("collectExportCharts 分节插图", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    snapshot.renderChartPng.mockResolvedValue(PNG);
    withResponse([]);
  });

  it("按 (messageId, stepIndex) 逐块截图，并跳过该轮顶层图", async () => {
    withResponse([multiStepMessage()]);

    const charts = await collectExportCharts("s-1");

    // kpi 步由后端原生排版，不截图；其余两步各一张
    expect(charts).toEqual([
      { messageId: 7, stepIndex: 0, imagePng: PNG },
      { messageId: 7, stepIndex: 2, imagePng: PNG },
    ]);
    // 顶层 option 一次都不该被渲染（否则 PDF 会多出一张重复的图）
    expect(snapshot.renderChartPng).toHaveBeenCalledTimes(2);
    expect(snapshot.renderChartPng).toHaveBeenNthCalledWith(1, STEP_OPTION_0);
    expect(snapshot.renderChartPng).toHaveBeenNthCalledWith(2, STEP_OPTION_2);
  });

  it("没有可分节块时仍走顶层图（回放数据缺 reportSections 的老消息）", async () => {
    withResponse([
      multiStepMessage({ reportSections: undefined, chartOption: { marker: "top" } }),
    ]);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([{ messageId: 7, imagePng: PNG }]);
    expect(snapshot.renderChartPng).toHaveBeenCalledWith({ marker: "top" });
  });

  it("分节图也按默认 Top-N 视图截图（与屏幕上看到的一致）", async () => {
    const truncated = {
      yAxis: { type: "category", data: Array.from({ length: 21 }, (_, i) => `C${i}`) },
      series: [{ type: "bar", data: [] }],
    };
    withResponse([
      multiStepMessage({
        stepsSnapshot: [
          {
            stepIndex: 0,
            chartType: "hbar",
            chartOption: {
              ...TALL_OPTION,
              topNOption: truncated,
              topNMeta: { limit: 20, hidden: 20, total: 40 },
            },
          },
        ],
        reportSections: [{ kind: "chart", stepIndex: 0, title: "步骤 1" }],
      }),
    ]);

    await collectExportCharts("s-1");

    expect(snapshot.renderChartPng).toHaveBeenCalledWith(truncated);
  });

  it("分节块指向快照里不存在的步骤时跳过该块，不影响其余", async () => {
    withResponse([
      multiStepMessage({
        reportSections: [
          { kind: "chart", stepIndex: 0, title: "步骤 1" },
          { kind: "chart", stepIndex: 9, title: "不存在" },
        ],
      }),
    ]);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([{ messageId: 7, stepIndex: 0, imagePng: PNG }]);
  });

  it("分节块指向白名单不认识的 chartType 时跳过", async () => {
    withResponse([
      multiStepMessage({
        stepsSnapshot: [{ stepIndex: 0, chartType: "radar-gl", chartOption: { series: [] } }],
        reportSections: [{ kind: "chart", stepIndex: 0, title: "步骤 1" }],
      }),
    ]);

    const charts = await collectExportCharts("s-1");

    expect(charts).toEqual([]);
  });
});
