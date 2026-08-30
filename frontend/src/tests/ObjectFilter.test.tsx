/** ObjectFilter 单元测试（Step 5 对象级下拉）。
 *
 * 覆盖：
 * - 纯函数 objectKey / collectObjectCandidates / filterEdgesByObjects
 *   （collectObjectCandidates 去重 (layer, object) + 计数 + 确定性排序；
 *    filterEdgesByObjects 选中对象只保留触及边、空选全保留、复合键匹配）
 * - 组件：按层分组渲染、多选回调复合键、清空回调空集、无候选时禁用
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ObjectFilter from "../components/lineage/ObjectFilter";
import {
  collectObjectCandidates,
  filterEdgesByObjects,
  objectKey,
  type ObjectCandidate,
} from "../components/lineage/lineageFilter";
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

describe("objectKey", () => {
  it("joins layer and object with a slash", () => {
    expect(objectKey("ODS", "ODS_PORDER")).toBe("ODS/ODS_PORDER");
  });
});

describe("collectObjectCandidates", () => {
  it("collects distinct (layer, object) pairs with edge counts", () => {
    const edges: LineageEdgeRead[] = [
      edge({ id: 1, sourceObject: "PORDER", targetObject: "BPSUPPLIER" }),
      edge({ id: 2, sourceObject: "BPSUPPLIER", targetObject: "ITMMASTER" }),
      edge({
        id: 3,
        sourceLayer: "SOURCE_SYSTEM",
        sourceObject: "PORDER",
        targetLayer: "ODS",
        targetObject: "ODS_PORDER",
      }),
    ];
    const candidates = collectObjectCandidates(edges);
    const countOf = (layer: string, obj: string) =>
      candidates.find((c) => c.layer === layer && c.object === obj)?.count ?? 0;
    expect(countOf("SOURCE_SYSTEM", "PORDER")).toBe(2);
    expect(countOf("SOURCE_SYSTEM", "BPSUPPLIER")).toBe(2);
    expect(countOf("SOURCE_SYSTEM", "ITMMASTER")).toBe(1);
    expect(countOf("ODS", "ODS_PORDER")).toBe(1);
    // 去重：4 个不同 (layer, object)
    expect(candidates.length).toBe(4);
  });

  it("sorts deterministically by layer then object", () => {
    const edges: LineageEdgeRead[] = [
      edge({ id: 1, sourceObject: "ZEBRA", targetObject: "PORDER" }),
    ];
    const candidates = collectObjectCandidates(edges);
    const names = candidates.map((c) => `${c.layer}/${c.object}`);
    expect(names).toEqual(["SOURCE_SYSTEM/PORDER", "SOURCE_SYSTEM/ZEBRA"]);
  });

  it("returns empty list for no edges", () => {
    expect(collectObjectCandidates([])).toEqual([]);
  });

  it("counts a self-loop edge once, not twice", () => {
    // source 与 target 是同一 (layer, object) → 只计一次，避免 label 显示 (2)
    const edges = [edge({ id: 1, sourceObject: "PORDER", targetObject: "PORDER" })];
    const candidates = collectObjectCandidates(edges);
    expect(candidates).toEqual([{ layer: "SOURCE_SYSTEM", object: "PORDER", count: 1 }]);
  });
});

describe("filterEdgesByObjects", () => {
  it("keeps all edges when no object selected", () => {
    const edges = [
      edge({ id: 1, sourceObject: "PORDER", targetObject: "BPSUPPLIER" }),
      edge({ id: 2, sourceObject: "BPSUPPLIER", targetObject: "ITMMASTER" }),
    ];
    expect(filterEdgesByObjects(edges, new Set())).toEqual(edges);
  });

  it("keeps only edges touching the selected object (source or target)", () => {
    const e1 = edge({ id: 1, sourceObject: "PORDER", targetObject: "BPSUPPLIER" });
    const e2 = edge({ id: 2, sourceObject: "BPSUPPLIER", targetObject: "ITMMASTER" });
    expect(filterEdgesByObjects([e1, e2], new Set(["SOURCE_SYSTEM/PORDER"]))).toEqual([e1]);
  });

  it("matches on both source and target composite keys", () => {
    const e1 = edge({ id: 1, sourceObject: "PORDER", targetObject: "BPSUPPLIER" });
    const e2 = edge({
      id: 2,
      sourceLayer: "SOURCE_SYSTEM",
      sourceObject: "PORDER",
      targetLayer: "ODS",
      targetObject: "ODS_PORDER",
    });
    expect(filterEdgesByObjects([e1, e2], new Set(["ODS/ODS_PORDER"]))).toEqual([e2]);
  });

  it("drops all edges when only stale keys selected", () => {
    const e1 = edge({ id: 1, sourceObject: "PORDER", targetObject: "BPSUPPLIER" });
    expect(filterEdgesByObjects([e1], new Set(["KPI/ODS_PORDER"]))).toEqual([]);
  });
});

describe("ObjectFilter component", () => {
  const candidates: ObjectCandidate[] = [
    { layer: "SOURCE_SYSTEM", object: "PORDER", count: 2 },
    { layer: "ODS", object: "ODS_PORDER", count: 1 },
  ];

  it("renders a multi-select scoped to candidates", () => {
    render(<ObjectFilter candidates={candidates} value={new Set()} onChange={vi.fn()} />);
    expect(document.querySelector(".ant-select")).toBeTruthy();
  });

  it("disables the select when there are no candidates", () => {
    render(<ObjectFilter candidates={[]} value={new Set()} onChange={vi.fn()} />);
    expect(document.querySelector(".ant-select-disabled")).toBeTruthy();
  });

  it("calls onChange with composite key when an option is chosen", async () => {
    const onChange = vi.fn();
    render(<ObjectFilter candidates={candidates} value={new Set()} onChange={onChange} />);
    const selector = document.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(selector);
    await screen.findByText(/^PORDER/);
    fireEvent.click(screen.getByText(/^PORDER/));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as Set<string>;
    expect(next.has("SOURCE_SYSTEM/PORDER")).toBe(true);
  });

  it("calls onChange with empty set when cleared", () => {
    const onChange = vi.fn();
    render(
      <ObjectFilter
        candidates={candidates}
        value={new Set(["SOURCE_SYSTEM/PORDER"])}
        onChange={onChange}
      />,
    );
    const clear = document.querySelector(".ant-select-clear") as HTMLElement;
    fireEvent.mouseDown(clear);
    expect(onChange).toHaveBeenCalledWith(new Set());
  });
});
