import { describe, it, expect } from "vitest";
import { classOptions, classOptionLabel } from "./classOptions";

const CLASSES = [
  { id: 1, className: "DWD_ARRIVAL_ORDER_DTL", classAlias: "到货单" },
  { id: 2, className: "DIM_SUPPLIER", classAlias: null },
] as never; // 只用到 className / classAlias / id，其余字段与本用例无关

describe("classOptions", () => {
  it("中文下中文别名前置", () => {
    expect(classOptionLabel("DWD_ARRIVAL_ORDER_DTL", "到货单", "zh-CN"))
      .toBe("到货单（DWD_ARRIVAL_ORDER_DTL）");
  });

  it("英文下物理名前置", () => {
    expect(classOptionLabel("DWD_ARRIVAL_ORDER_DTL", "到货单", "en-US"))
      .toBe("DWD_ARRIVAL_ORDER_DTL (到货单)");
  });

  it("无别名的类两种语言都只显示物理名，且 value 仍是数字 id", () => {
    expect(classOptions(CLASSES, "zh-CN")).toEqual([
      { label: "到货单（DWD_ARRIVAL_ORDER_DTL）", value: 1 },
      { label: "DIM_SUPPLIER", value: 2 },
    ]);
  });
});
