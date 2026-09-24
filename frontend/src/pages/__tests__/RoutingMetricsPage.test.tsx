/** RoutingMetricsPage tests (Task 5.3 — feat-complex-metric-pipeline).
 *
 * RED phase: tests define the expected UI behavior.
 * Mock fetch returns RoutingMetricsSnapshot shape; no real backend needed.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import RoutingMetricsPage from "../RoutingMetricsPage";
import type { RoutingMetricsSnapshot } from "../../types/routingMetrics";

// Mock echarts-for-react to avoid canvas rendering in jsdom
vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: vi.fn(({ style }) => (
    <div data-testid="echarts-canvas" style={style}>
      <canvas />
    </div>
  )),
}));

const mockSnapshot: RoutingMetricsSnapshot = {
  since: "2026-09-01T00:00:00Z",
  until: "2026-09-10T23:59:59Z",
  layerDistribution: [
    { layer: "L1", hitCount: 4200, avgDurationMs: 12.5, avgTokenCost: 0.001 },
    { layer: "L2", hitCount: 2800, avgDurationMs: 145.3, avgTokenCost: 0.018 },
    { layer: "L3", hitCount: 800, avgDurationMs: 532.1, avgTokenCost: 0.067 },
    { layer: "L4", hitCount: 200, avgDurationMs: 1820.0, avgTokenCost: 0.245 },
  ],
  totalQueries: 8000,
  avgTotalDurationMs: 187.4,
};

const emptySnapshot: RoutingMetricsSnapshot = {
  since: "2026-09-01T00:00:00Z",
  until: "2026-09-10T23:59:59Z",
  layerDistribution: [
    { layer: "L1", hitCount: 0, avgDurationMs: 0, avgTokenCost: 0 },
    { layer: "L2", hitCount: 0, avgDurationMs: 0, avgTokenCost: 0 },
    { layer: "L3", hitCount: 0, avgDurationMs: 0, avgTokenCost: 0 },
    { layer: "L4", hitCount: 0, avgDurationMs: 0, avgTokenCost: 0 },
  ],
  totalQueries: 0,
  avgTotalDurationMs: 0,
};

function renderPage(_snapshot: RoutingMetricsSnapshot = mockSnapshot) {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <RoutingMetricsPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
}

describe("RoutingMetricsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(global, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify(mockSnapshot), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  it("renders 4 layer cards (L1/L2/L3/L4) with hit counts", async () => {
    renderPage();
    // Exact i18n labels for each layer (text is wrapped in react-i18next <span>)
    const layerLabels = ["L1 语义匹配", "L2 LLM 意图分类", "L3 多步链式推理", "L4 LangGraph Agent"];
    for (const label of layerLabels) {
      expect(await screen.findByText(label)).toBeInTheDocument();
    }
    // Hit counts from mock snapshot (layer card numbers)
    expect(await screen.findByText("4,200")).toBeInTheDocument(); // L1
    expect(await screen.findByText("2,800")).toBeInTheDocument(); // L2
    expect(await screen.findByText("800")).toBeInTheDocument();   // L3
    expect(await screen.findByText("200")).toBeInTheDocument();   // L4
  });

  it("renders ECharts canvas elements in both chart cards", async () => {
    renderPage();
    // Two echarts instances: pie + line
    await waitFor(() => {
      const canvases = screen.getAllByTestId("echarts-canvas");
      expect(canvases.length).toBe(2);
    });
  });

  it("renders time window selector (1d/7d/30d)", async () => {
    renderPage();
    const select = await screen.findByRole("combobox");
    expect(select).toBeInTheDocument();
    await userEvent.click(select);
    const options = await screen.findAllByRole("option");
    expect(options.length).toBeGreaterThanOrEqual(3);
  });

  it("renders zero hit counts when totalQueries is 0", async () => {
    vi.spyOn(global, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify(emptySnapshot), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    renderPage(emptySnapshot);
    await waitFor(() => {
      // Empty state: all layer cards show 0
      expect(screen.getAllByText(/\b0\b/).length).toBeGreaterThanOrEqual(4);
    });
  });

  it("renders error alert on fetch failure", async () => {
    vi.spyOn(global, "fetch").mockRejectedValueOnce(new Error("Network error"));
    renderPage();
    await waitFor(() => {
      // Alert renders with class ant-alert-error and message "加载失败"
      const alert = document.querySelector(".ant-alert");
      expect(alert).toBeInTheDocument();
    });
  });
});
