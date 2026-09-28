/**
 * ruleCodeGenerator 单测（feat-rule-batch-create，2026-09-15）
 *
 * 覆盖：
 * - 编码拼接：MU-DQ-PURCHASE_ORDER-20260915-00001
 * - 类名清洗：非 ASCII 字符丢弃；超长截断到 12；纯特殊字符 → CLASS
 * - YYYYMMDD 格式化
 * - 流水号 5 位补零
 * - 规则名 buildRuleName：{类}-{列}-{规则类型中文}
 * - parseRuleCode 解析合法编码；非法编码返回 null
 */
import { describe, it, expect } from "vitest";
import {
  buildRuleCode,
  buildRuleName,
  formatYmd,
  padSeq,
  parseRuleCode,
  sanitizeClassName,
} from "../utils/ruleCodeGenerator";

describe("ruleCodeGenerator（feat-rule-batch-create）", () => {
  it("buildRuleCode: MU-DQ-PURCHASE_ORDER-20260915-00001", () => {
    expect(
      buildRuleCode({
        className: "PURCHASE_ORDER",
        seq: 1,
        date: new Date(2026, 8, 15), // 月份 0-indexed: 8=9 月
      })
    ).toBe("MU-DQ-PURCHASE_ORDER-20260915-00001");
  });

  it("sanitizeClassName: 非 ASCII 字符丢弃", () => {
    expect(sanitizeClassName("采购订单")).toBe("CLASS");
    expect(sanitizeClassName("采购PURCHASE")).toBe("PURCHASE");
    // PURCHASE_ORDER = 14 字符，默认 maxLen=12 → 截断
    expect(sanitizeClassName("Purchase Order")).toBe("PURCHASE_ORD");
    // 显式提高 maxLen 才能完整保留
    expect(sanitizeClassName("Purchase Order", 30)).toBe("PURCHASE_ORDER");
  });

  it("sanitizeClassName: 超长截断到 12 字符", () => {
    const long = "VERY_LONG_CLASS_NAME_THAT_EXCEEDS";
    expect(sanitizeClassName(long).length).toBe(12);
    expect(sanitizeClassName(long)).toBe("VERY_LONG_CL");
  });

  it("formatYmd: 默认今天 + 自定义日期", () => {
    const d = new Date(2026, 0, 5); // 1 月 5 日
    expect(formatYmd(d)).toBe("20260105");
  });

  it("padSeq: 5 位补零", () => {
    expect(padSeq(1)).toBe("00001");
    expect(padSeq(123)).toBe("00123");
    expect(padSeq(99999)).toBe("99999");
    // 0 / 负数兜底为 1
    expect(padSeq(0)).toBe("00001");
    expect(padSeq(-5)).toBe("00001");
  });

  it("buildRuleName: 类-列-规则类型中文", () => {
    expect(
      buildRuleName({
        className: "采购订单",
        columnName: "PO_QTY",
        ruleType: "VALIDITY",
      })
    ).toBe("采购订单-PO_QTY-有效性");
    expect(
      buildRuleName({
        className: "供应商",
        columnName: "SUPPLIER_NAME",
        ruleType: "UNIQUENESS",
      })
    ).toBe("供应商-SUPPLIER_NAME-唯一性");
  });

  it("parseRuleCode: 合法编码解析", () => {
    expect(
      parseRuleCode("MU-DQ-PURCHASE_ORDER-20260915-00007")
    ).toEqual({
      className: "PURCHASE_ORDER",
      date: "20260915",
      seq: 7,
    });
  });

  it("parseRuleCode: 非法编码 → null", () => {
    expect(parseRuleCode("PO_QTY_NON_NEG")).toBeNull();
    expect(parseRuleCode("MU-DQ-X-20260915-7")).toBeNull(); // 流水不足 5 位
  });
});