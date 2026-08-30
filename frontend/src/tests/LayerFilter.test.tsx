/** LayerFilter 单元测试（Phase 2.3 RED）。
 *
 * 受控多选层筛选器：
 * - 渲染 7 层 Checkbox.Group
 * - 默认全选
 * - 改变时回调 onChange(new Set<LineageLayer>)
 * - 受控：value 由父组件传入
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import LayerFilter, { ALL_LAYERS, layerColor } from "../components/lineage/LayerFilter";

describe("LayerFilter", () => {
  it("renders all 7 lineage layers", () => {
    const onChange = vi.fn();
    render(<LayerFilter value={new Set(ALL_LAYERS)} onChange={onChange} />);
    for (const layer of ALL_LAYERS) {
      expect(screen.getByText(layer)).toBeTruthy();
    }
  });

  it("calls onChange when user toggles a layer off", () => {
    const onChange = vi.fn();
    render(<LayerFilter value={new Set(ALL_LAYERS)} onChange={onChange} />);
    // 取消勾选 SOURCE_SYSTEM
    const checkbox = screen.getByLabelText("SOURCE_SYSTEM") as HTMLInputElement;
    fireEvent.click(checkbox);
    expect(onChange).toHaveBeenCalled();
    const newValue = onChange.mock.calls[0][0] as Set<string>;
    expect(newValue.has("SOURCE_SYSTEM")).toBe(false);
    expect(newValue.size).toBe(6);
  });

  it("calls onChange with new set when user toggles a layer on", () => {
    const onChange = vi.fn();
    const initial = new Set<typeof ALL_LAYERS[number]>(ALL_LAYERS);
    initial.delete("KPI");
    render(<LayerFilter value={initial} onChange={onChange} />);
    const checkbox = screen.getByLabelText("KPI") as HTMLInputElement;
    fireEvent.click(checkbox);
    const newValue = onChange.mock.calls[0][0] as Set<string>;
    expect(newValue.has("KPI")).toBe(true);
    expect(newValue.size).toBe(ALL_LAYERS.length);
  });
});

describe("ALL_LAYERS constant", () => {
  it("contains exactly 7 layers in canonical order", () => {
    expect(ALL_LAYERS).toEqual([
      "SOURCE_SYSTEM",
      "ODS",
      "DWD",
      "DWS",
      "ADS",
      "KPI",
      "AI",
    ]);
  });
});

describe("layerColor helper", () => {
  it("returns distinct colors for different layers", () => {
    const c1 = layerColor("SOURCE_SYSTEM");
    const c2 = layerColor("ODS");
    expect(c1).not.toBe(c2);
  });

  it("returns a hex color string", () => {
    expect(layerColor("KPI")).toMatch(/^#[0-9a-fA-F]{6}$/);
  });
});
