import { describe, expect, it } from "vitest";
import { summarizeRuleParams } from "../utils/ruleParamsSummary";

describe("summarizeRuleParams", () => {
  it("not_null", () => {
    expect(summarizeRuleParams({ kind: "not_null" }, "PO_LINE_KEY", "zh-CN"))
      .toBe("PO_LINE_KEY 非空");
  });

  it("unique", () => {
    expect(summarizeRuleParams({ kind: "unique" }, "PO_LINE_KEY", "zh-CN"))
      .toBe("PO_LINE_KEY 唯一");
  });

  it("range both", () => {
    expect(summarizeRuleParams(
      { kind: "range", min: 0, max: 100 }, "ORDER_QTY", "zh-CN",
    )).toBe("ORDER_QTY ∈ [0, 100]");
  });

  it("range min only", () => {
    expect(summarizeRuleParams({ kind: "range", min: 0 }, "QTY", "zh-CN"))
      .toBe("QTY ≥ 0");
  });

  it("in_set", () => {
    expect(summarizeRuleParams({ kind: "in_set", values: ["A", "B"] }, "STATUS", "zh-CN"))
      .toBe("STATUS ∈ {A, B}");
  });

  it("regex", () => {
    expect(summarizeRuleParams({ kind: "regex", pattern: "^[0-9]+$" }, "ORDER_NO", "zh-CN"))
      .toBe("ORDER_NO 匹配 ^[0-9]+$");
  });

  it("compare gt", () => {
    expect(summarizeRuleParams({ kind: "compare", op: ">", value: 0 }, "PRICE", "zh-CN"))
      .toBe("PRICE > 0");
  });

  it("ref", () => {
    expect(summarizeRuleParams(
      { kind: "ref", ref_table: "SUPPLIER", ref_column: "SUPPLIER_KEY" }, "SUPPLIER_KEY", "zh-CN",
    )).toBe("SUPPLIER_KEY 参照 SUPPLIER.SUPPLIER_KEY");
  });

  it("cross_column with factor", () => {
    expect(summarizeRuleParams({
      kind: "cross_column",
      left: "RECEIVED_QTY", op: "<=", right: "ORDER_QTY", factor: 1.05,
    }, "RECEIVED_QTY", "zh-CN")).toBe("RECEIVED_QTY ≤ ORDER_QTY × 1.05");
  });

  it("cross_column no factor", () => {
    expect(summarizeRuleParams({
      kind: "cross_column", left: "A", op: "=", right: "B",
    }, "A", "zh-CN")).toBe("A = B");
  });

  it("en locale", () => {
    expect(summarizeRuleParams({ kind: "not_null" }, "X", "en-US"))
      .toBe("X IS NOT NULL");
  });
});
