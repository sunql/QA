import { describe, it, expect } from "vitest";
import { parseKpiPayload } from "../components/chat/KpiCard";

// parseKpiPayload 是系统边界：chartOption 来自后端 JSON，形状不可信。
// 它同时决定「这张卡要不要渲染」——判错的表现是要么弹出一个空壳卡（value 为 NaN），
// 要么把后端真发来的值吞掉（数值字符串）。
describe("parseKpiPayload", () => {
  it("接受标准负载（数字 value）", () => {
    expect(parseKpiPayload({ kpi: { label: "收货量", value: 9812, unit: "件", delta: 12 } })).toEqual({
      label: "收货量",
      value: 9812,
      unit: "件",
      delta: 12,
    });
  });

  it("接受数值字符串（JSON 各种来源都可能给出 \"0.954\"）", () => {
    expect(parseKpiPayload({ kpi: { label: "及时交货率", value: "0.954" } })).toMatchObject({
      value: 0.954,
    });
  });

  it("空字符串不算数值（Number('') === 0 会把空值画成 0）", () => {
    expect(parseKpiPayload({ kpi: { label: "收货量", value: "" } })).toBeNull();
  });

  it("NaN / Infinity 不渲染（antd Statistic 会把它们画成 NaN 字样）", () => {
    expect(parseKpiPayload({ kpi: { label: "收货量", value: Number.NaN } })).toBeNull();
    expect(parseKpiPayload({ kpi: { label: "收货量", value: Number.POSITIVE_INFINITY } })).toBeNull();
    expect(parseKpiPayload({ kpi: { label: "收货量", value: "NaN" } })).toBeNull();
  });

  it("label 缺失或非字符串时不渲染", () => {
    expect(parseKpiPayload({ kpi: { value: 1 } })).toBeNull();
    expect(parseKpiPayload({ kpi: { label: 1, value: 1 } })).toBeNull();
  });

  it("kpi 缺失 / 非对象 / 数组时不渲染", () => {
    expect(parseKpiPayload(null)).toBeNull();
    expect(parseKpiPayload({})).toBeNull();
    expect(parseKpiPayload({ kpi: "收货量" })).toBeNull();
    expect(parseKpiPayload({ kpi: [1, 2] })).toBeNull();
  });

  it("unit / delta 非字符串/数字时降级为 null（不拖垮整张卡）", () => {
    expect(parseKpiPayload({ kpi: { label: "收货量", value: 1, unit: 3, delta: "x" } })).toEqual({
      label: "收货量",
      value: 1,
      unit: null,
      delta: null,
    });
  });
});
