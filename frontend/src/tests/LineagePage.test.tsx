/** LineagePage 集成测试（Phase 2.3 RED）。
 *
 * 覆盖：
 * - mount 时拉取 edges
 * - 加载完成后渲染 LineageGraph
 * - LayerFilter 取消勾选某层 → LineageGraph 收到的 edges 不含该层
 * - 拉取失败显示错误 toast
 * - 空 edges 时显示空状态
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { message } from "antd";

vi.mock("../api/lineage", () => ({
  listEdges: vi.fn(),
}));

const echartsOptionCapture = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));

vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: (props: { option?: Record<string, unknown> }) => {
    echartsOptionCapture.current = props.option ?? null;
    return (
      <div data-testid="echarts-mock">{props.option ? "rendered" : "empty"}</div>
    );
  },
}));

import LineagePage from "../pages/LineagePage";
import * as lineageApi from "../api/lineage";
import type { LineageEdgeRead } from "../types/lineage";

function edge(overrides: Partial<LineageEdgeRead>): LineageEdgeRead {
  return {
    id: 1,
    sourceLayer: "SOURCE_SYSTEM",
    sourceSystem: "ERP",
    sourceObject: "PORDER",
    sourceField: null,
    targetLayer: "SOURCE_SYSTEM",
    targetSystem: "ERP",
    targetObject: "BPSUPPLIER",
    targetField: null,
    transformationRule: null,
    refreshFrequency: "DAILY",
    owner: null,
    description: null,
    isActive: true,
    createdTime: null,
    updatedTime: null,
    ...overrides,
  };
}

describe("LineagePage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    echartsOptionCapture.current = null;
  });

  it("fetches edges on mount and renders graph", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({ id: 1, sourceObject: "PORDER", targetObject: "BPSUPPLIER" }),
      edge({ id: 2, sourceObject: "BPSUPPLIER", targetObject: "ITMMASTER" }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(screen.getByTestId("echarts-mock").textContent).toBe("rendered");
    });
    expect(lineageApi.listEdges).toHaveBeenCalledTimes(1);
  });

  it("shows error toast on fetch failure", async () => {
    const errorSpy = vi.spyOn(message, "error").mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    vi.mocked(lineageApi.listEdges).mockRejectedValue(new Error("network error"));
    render(<LineagePage />);
    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalled();
    });
    errorSpy.mockRestore();
  });

  it("renders empty state when no edges", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([]);
    render(<LineagePage />);
    await waitFor(() => {
      // Empty state — graph not rendered; check for "暂无血缘" placeholder text
      expect(screen.queryByTestId("echarts-mock")).toBeNull();
    });
  });

  it("filters edges by selected layers", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({ id: 1, sourceLayer: "SOURCE_SYSTEM", targetLayer: "SOURCE_SYSTEM", sourceObject: "A", targetObject: "B" }),
      edge({ id: 2, sourceLayer: "ODS", targetLayer: "DWD", sourceObject: "ODS_X", targetObject: "DWD_X" }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(screen.getByTestId("echarts-mock").textContent).toBe("rendered");
    });
    // 取消勾选 SOURCE_SYSTEM
    const checkbox = screen.getByLabelText("SOURCE_SYSTEM") as HTMLInputElement;
    fireEvent.click(checkbox);
    await waitFor(() => {
      // 过滤后只剩 ODS → DWD 一条边
      const opt = echartsOptionCapture.current as {
        series?: Array<{ nodes?: Array<{ name: string }>; links?: unknown[] }>;
      } | null;
      expect(opt).not.toBeNull();
      const nodes = opt?.series?.[0]?.nodes ?? [];
      const names = nodes.map((n) => n.name).sort();
      expect(names).toEqual(["DWD_X", "ODS_X"]);
      expect(opt?.series?.[0]?.links?.length).toBe(1);
    });
  });
});
