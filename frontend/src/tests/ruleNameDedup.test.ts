/**
 * ruleNameDedup 单测（feat-rule-batch-create，2026-09-15）
 *
 * 覆盖：
 * - 5 条规则名有 2 条重复 → 返回两个索引
 * - 全 unique → 空数组
 * - 三条同名 → 返回三个索引
 */
import { describe, it, expect } from "vitest";
import { findDuplicateNameIndices } from "../utils/ruleNameDedup";

describe("ruleNameDedup（feat-rule-batch-create）", () => {
  it("5 条规则 2 条同名 → 返回两个索引", () => {
    const result = findDuplicateNameIndices([
      { ruleName: "采购订单-PO_QTY-有效性" },
      { ruleName: "采购订单-UNIT_PR-有效性" },
      { ruleName: "采购订单-PO_QTY-有效性" }, // dup 1,2
      { ruleName: "采购订单-START_DATE-有效性" },
      { ruleName: "采购订单-PO_QTY-有效性" }, // dup 1,2,4
    ]);
    expect(result).toEqual([0, 2, 4]);
  });

  it("全 unique → 空数组", () => {
    const result = findDuplicateNameIndices([
      { ruleName: "A-1-完整性" },
      { ruleName: "A-2-有效性" },
      { ruleName: "B-3-唯一性" },
    ]);
    expect(result).toEqual([]);
  });

  it("三条同名 → 返回三个索引", () => {
    const result = findDuplicateNameIndices([
      { ruleName: "X" },
      { ruleName: "Y" },
      { ruleName: "X" },
      { ruleName: "X" },
    ]);
    expect(result).toEqual([0, 2, 3]);
  });

  it("空数组 → 空", () => {
    expect(findDuplicateNameIndices([])).toEqual([]);
  });
});