/** LineageGraph 单元测试（Phase 2.3 RED）。
 *
 * 覆盖 edgesToGraphOption 纯函数 + LineageGraph 组件：
 * - 空 edges → nodes 与 links 都为空
 * - 单边 → 2 nodes + 1 link
 * - 多边共享节点 → 节点去重
 * - 层 → 颜色映射（SOURCE_SYSTEM / ODS / KPI 等）
 * - active=false 边渲染为虚线
 * - 字段级 vs 表级血缘（sourceField 非 null 时节点名带 .field）
 * - 组件正确把 option 透传给 ReactECharts（mock 校验）
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import type { LineageEdgeRead } from "../types/lineage";

// 全局 echarts mock：暴露传入的 option 供断言
const echartsOptionCapture = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));

vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: React.forwardRef(function EChartsMock(
    props: { option?: Record<string, unknown>; style?: React.CSSProperties },
    ref: React.ForwardedRef<unknown>,
  ) {
    React.useImperativeHandle(ref, () => ({
      getEchartsInstance: () => ({ getDataURL: () => "data:image/png;base64,x" }),
    }));
    echartsOptionCapture.current = props.option ?? null;
    return (
      <div data-testid="echarts-mock" style={props.style}>
        {props.option ? JSON.stringify(props.option) : null}
      </div>
    );
  }),
}));

import LineageGraph, { edgesToGraphOption } from "../components/lineage/LineageGraph";

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

// ---- edgesToGraphOption 纯函数 ----

describe("edgesToGraphOption", () => {
  it("returns empty nodes and links for empty input", () => {
    const opt = edgesToGraphOption([]);
    expect(opt.nodes).toEqual([]);
    expect(opt.links).toEqual([]);
  });

  it("creates two nodes and one link from a single edge", () => {
    const opt = edgesToGraphOption([
      edge({
        id: 1,
        sourceObject: "PORDER",
        sourceField: "BPSNUM",
        targetObject: "BPSUPPLIER",
        targetField: "BPSNUM",
      }),
    ]);
    expect(opt.nodes).toHaveLength(2);
    expect(opt.links).toHaveLength(1);
    const link = opt.links![0] as { source: string; target: string };
    expect(link.source).toMatch(/PORDER/);
    expect(link.target).toMatch(/BPSUPPLIER/);
  });

  it("dedups nodes shared across multiple edges", () => {
    const opt = edgesToGraphOption([
      edge({ id: 1, sourceObject: "TA", targetObject: "TB", sourceField: "K1", targetField: "K1" }),
      edge({ id: 2, sourceObject: "TA", targetObject: "TB", sourceField: "K2", targetField: "K2" }),
    ]);
    // TA.K1, TA.K2 → TB.K1, TB.K2 — 4 distinct nodes (field-level nodes are unique)
    expect(opt.nodes).toHaveLength(4);
    expect(opt.links).toHaveLength(2);
  });

  it("uses one table-level node per object when sourceField is null", () => {
    const opt = edgesToGraphOption([
      edge({ id: 1, sourceObject: "TA", sourceField: null, targetObject: "TB", targetField: null }),
      edge({ id: 2, sourceObject: "TA", sourceField: null, targetObject: "TB", targetField: null }),
    ]);
    // Both edges reference TA and TB → 2 distinct nodes
    expect(opt.nodes).toHaveLength(2);
  });

  it("maps each layer to a distinct color", () => {
    const opt = edgesToGraphOption([
      edge({ id: 1, sourceLayer: "SOURCE_SYSTEM", targetLayer: "ODS", sourceObject: "A", targetObject: "B" }),
    ]);
    const colors = new Set((opt.nodes as Array<{ itemStyle?: { color?: string } }>).map((n) => n.itemStyle?.color));
    expect(colors.size).toBe(2); // SOURCE_SYSTEM color != ODS color
  });

  it("renders inactive edge as dashed line", () => {
    const opt = edgesToGraphOption([
      edge({ id: 1, isActive: false, sourceObject: "TA", targetObject: "TB" }),
    ]);
    const link = opt.links![0] as { lineStyle?: { type?: string } };
    expect(link.lineStyle?.type).toBe("dashed");
  });

  it("renders active edge as solid line", () => {
    const opt = edgesToGraphOption([
      edge({ id: 1, isActive: true, sourceObject: "TA", targetObject: "TB" }),
    ]);
    const link = opt.links![0] as { lineStyle?: { type?: string } };
    expect(link.lineStyle?.type).not.toBe("dashed");
  });

  it("includes transformationRule as edge label when present", () => {
    const opt = edgesToGraphOption([
      edge({ id: 1, transformationRule: "SUM(t.QTY)", sourceObject: "TA", targetObject: "TB" }),
    ]);
    const link = opt.links![0] as { label?: { show?: boolean; formatter?: unknown } };
    expect(link.label?.show).toBe(true);
    expect(String(link.label?.formatter)).toContain("SUM");
  });
});

// ---- LineageGraph 组件 ----

describe("LineageGraph component", () => {
  beforeEach(() => {
    echartsOptionCapture.current = null;
  });

  it("renders echarts with computed option", () => {
    render(<LineageGraph edges={[edge({ id: 1, sourceObject: "A", targetObject: "B" })]} />);
    expect(screen.getByTestId("echarts-mock")).toBeTruthy();
    const opt = echartsOptionCapture.current;
    expect(opt).not.toBeNull();
    // ECharts graph wraps nodes inside series[0]
    const series = (opt as { series?: Array<{ nodes?: unknown[] }> }).series ?? [];
    expect(series[0]?.nodes?.length).toBe(2);
  });

  it("returns null when no edges", () => {
    const { container } = render(<LineageGraph edges={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("applies custom height via style prop", () => {
    render(<LineageGraph edges={[edge({ id: 1 })]} height={500} />);
    const node = screen.getByTestId("echarts-mock");
    expect((node as HTMLElement).style.height).toBe("500px");
  });
});
