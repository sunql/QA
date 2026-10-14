import { describe, it, expect } from "vitest";
import { ontologyObjectLabel } from "./ontologyLabel";

describe("ontologyObjectLabel", () => {
  it("中文下别名在前：别名（物理名）", () => {
    expect(ontologyObjectLabel("DWD_ARRIVAL_ORDER_DTL", "到货单", "zh-CN"))
      .toBe("到货单（DWD_ARRIVAL_ORDER_DTL）");
  });

  it("英文下物理名在前：物理名 (alias)", () => {
    expect(ontologyObjectLabel("DWD_ARRIVAL_ORDER_DTL", "到货单", "en-US"))
      .toBe("DWD_ARRIVAL_ORDER_DTL (到货单)");
  });

  it("无别名时只显示物理名（两种语言一致）", () => {
    expect(ontologyObjectLabel("DIM_SUPPLIER", null, "zh-CN")).toBe("DIM_SUPPLIER");
    expect(ontologyObjectLabel("DIM_SUPPLIER", undefined, "en-US")).toBe("DIM_SUPPLIER");
  });

  it("空字符串别名按无别名处理", () => {
    expect(ontologyObjectLabel("DIM_SUPPLIER", "", "zh-CN")).toBe("DIM_SUPPLIER");
  });
});
