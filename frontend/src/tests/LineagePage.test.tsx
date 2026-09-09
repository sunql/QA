/** LineagePage 集成测试（Phase 2.3 RED + Step 5 对象级下拉）。
 *
 * 覆盖：
 * - mount 时拉取 edges
 * - 加载完成后渲染 LineageGraph
 * - LayerFilter 取消勾选某层 → LineageGraph 收到的 edges 不含该层
 * - 拉取失败显示错误 toast
 * - 空 edges 时显示空状态
 * - ObjectFilter 候选来自层过滤后的 edges（按层排序）
 * - 选中对象 → LineageGraph 只渲染触及该对象的边
 * - 取消某层 → 该层对象从候选移除，且对象选择被裁剪
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { message } from "antd";

vi.mock("../api/lineage", () => ({
  listEdges: vi.fn(),
  extractLineage: vi.fn(),
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

// ObjectFilter 只 mock 组件外壳；纯函数（collectObjectCandidates / filterEdgesByObjects）
// 用真实实现，LineagePage 的接线逻辑才能被真实执行。
const objectFilterProps = vi.hoisted(() => ({
  current: null as {
    candidates: Array<{ layer: string; object: string; count: number }>;
    value: Set<string>;
    onChange: (next: Set<string>) => void;
  } | null,
}));

vi.mock("../components/lineage/ObjectFilter", () => ({
  __esModule: true,
  default: (props: {
    candidates: Array<{ layer: string; object: string; count: number }>;
    value: Set<string>;
    onChange: (next: Set<string>) => void;
  }) => {
    objectFilterProps.current = props;
    return <div data-testid="object-filter-mock" />;
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
    objectFilterProps.current = null;
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

  it("non-Error rejection 走 String(error) 分支（errorMessageOf 兜底）", async () => {
    const errorSpy = vi.spyOn(message, "error").mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    vi.mocked(lineageApi.listEdges).mockRejectedValue("plain string error");
    render(<LineagePage />);
    await waitFor(() => {
      // 错误字符串透传
      expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("plain string error"));
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

  it("passes object candidates derived from layer-filtered edges", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({
        id: 1,
        sourceLayer: "SOURCE_SYSTEM",
        targetLayer: "SOURCE_SYSTEM",
        sourceObject: "PORDER",
        targetObject: "BPSUPPLIER",
      }),
      edge({
        id: 2,
        sourceLayer: "ODS",
        targetLayer: "DWD",
        sourceObject: "ODS_PORDER",
        targetObject: "DWD_X",
      }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(objectFilterProps.current).not.toBeNull();
    });
    // 候选按层名排序：DWD < ODS < SOURCE_SYSTEM
    const layers = objectFilterProps.current!.candidates.map((c) => c.layer);
    expect(layers).toEqual(["DWD", "ODS", "SOURCE_SYSTEM", "SOURCE_SYSTEM"]);
    const objects = objectFilterProps.current!.candidates.map((c) => c.object).sort();
    expect(objects).toEqual(["BPSUPPLIER", "DWD_X", "ODS_PORDER", "PORDER"]);
  });

  it("narrows graph to edges touching the selected object", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({ id: 1, sourceObject: "PORDER", targetObject: "BPSUPPLIER" }),
      edge({ id: 2, sourceObject: "BPSUPPLIER", targetObject: "ITMMASTER" }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(screen.getByTestId("echarts-mock").textContent).toBe("rendered");
    });
    // 模拟用户在 ObjectFilter 选中 PORDER（纯函数被真实执行）
    objectFilterProps.current!.onChange(new Set(["SOURCE_SYSTEM/PORDER"]));
    await waitFor(() => {
      const opt = echartsOptionCapture.current as {
        series?: Array<{ nodes?: Array<{ name: string }> }>;
      } | null;
      const names = (opt?.series?.[0]?.nodes ?? []).map((n) => n.name).sort();
      // 只剩 PORDER → BPSUPPLIER 一条边；ITMMASTER 不再出现
      expect(names).toEqual(["BPSUPPLIER", "PORDER"]);
    });
  });

  it("extract: created>0 → toast 新增数 + 拉取刷新", async () => {
    const successSpy = vi
      .spyOn(message, "success")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.success>);
    vi.mocked(lineageApi.listEdges).mockResolvedValue([]);
    vi.mocked(lineageApi.extractLineage).mockResolvedValue({ created: 3 });
    render(<LineagePage />);
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: /自动抽取血缘/ }));

    await waitFor(() => {
      expect(lineageApi.extractLineage).toHaveBeenCalledTimes(1);
    });
    expect(successSpy).toHaveBeenCalledWith(expect.stringContaining("3"));
    // 抽取成功后自动 refresh → 第二次 listEdges
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(2);
    });
    successSpy.mockRestore();
  });

  it("extract: created=0 → info 提示无新增（幂等）且仍刷新", async () => {
    const infoSpy = vi
      .spyOn(message, "info")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.info>);
    vi.mocked(lineageApi.listEdges).mockResolvedValue([]);
    vi.mocked(lineageApi.extractLineage).mockResolvedValue({ created: 0 });
    render(<LineagePage />);
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: /自动抽取血缘/ }));

    await waitFor(() => {
      expect(infoSpy).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(2);
    });
    infoSpy.mockRestore();
  });

  it("extract failure → error toast，不崩溃", async () => {
    const errorSpy = vi
      .spyOn(message, "error")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    vi.mocked(lineageApi.listEdges).mockResolvedValue([]);
    vi.mocked(lineageApi.extractLineage).mockRejectedValue(new Error("extract boom"));
    render(<LineagePage />);
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: /自动抽取血缘/ }));

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("extract boom"));
    });
    errorSpy.mockRestore();
  });

  it("prunes selected objects when their layer is deselected", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({
        id: 1,
        sourceLayer: "SOURCE_SYSTEM",
        targetLayer: "SOURCE_SYSTEM",
        sourceObject: "PORDER",
        targetObject: "BPSUPPLIER",
      }),
      edge({
        id: 2,
        sourceLayer: "ODS",
        targetLayer: "DWD",
        sourceObject: "ODS_PORDER",
        targetObject: "DWD_X",
      }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(objectFilterProps.current).not.toBeNull();
    });
    // 先选中 SOURCE_SYSTEM 层的 PORDER
    objectFilterProps.current!.onChange(new Set(["SOURCE_SYSTEM/PORDER"]));
    // 再取消 SOURCE_SYSTEM 层
    const checkbox = screen.getByLabelText("SOURCE_SYSTEM") as HTMLInputElement;
    fireEvent.click(checkbox);
    await waitFor(() => {
      const layers = objectFilterProps.current!.candidates.map((c) => c.layer);
      expect(layers).toEqual(["DWD", "ODS"]);
      // effectiveSelected 裁剪：PORDER 已不在候选 → value 为空
      expect(objectFilterProps.current!.value.size).toBe(0);
    });
  });
});
