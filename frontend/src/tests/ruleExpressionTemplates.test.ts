/**
 * ruleExpressionTemplates 单测（feat-rule-batch-create，2026-09-15）
 *
 * 覆盖：
 * - NUMBER + VALIDITY + 列名含 QTY → "> 0"
 * - NUMBER + VALIDITY + 列名含 PRICE → ">= 0"
 * - NUMBER + VALIDITY + 列名 OTHER + 本体属性给 min/max → "BETWEEN ... AND ..."
 * - NUMBER + VALIDITY + 列名 OTHER + 本体属性无 min/max → null
 * - VARCHAR + VALIDITY + 列名含 NO → REGEX
 * - VARCHAR + UNIQUENESS → null（无 expression，依赖 DB 唯一约束）
 * - VARCHAR + COMPLETENESS → `${col} IS NOT NULL`（与生成器对齐）
 * - NUMBER + COMPLETENESS → `${col} IS NOT NULL`
 * - DATE + VALIDITY + 列名含 START_DATE → "<= CURRENT_TIMESTAMP"
 * - DATE + VALIDITY + 列名含 EXPIRE_DATE + 本体 min → ">= '2020-01-01'"
 * - REFERENTIAL + 本体属性给 ref → "REF <table>.<col>"
 * - REFERENTIAL + 本体属性未给 ref → null
 * - TIMELINESS → null（未实现）
 */
import { describe, it, expect } from "vitest";
import { suggestRuleExpression } from "../utils/ruleExpressionTemplates";

describe("ruleExpressionTemplates（feat-rule-batch-create）", () => {
  it("NUMBER + VALIDITY + 列名含 QTY → > 0", () => {
    const r = suggestRuleExpression({
      column: { name: "PO_QTY", dataType: "NUMBER" },
      ruleType: "VALIDITY",
    });
    expect(r.canAutoFill).toBe(true);
    expect(r.expression).toBe("PO_QTY > 0");
    expect(r.templateId).toBe("T_NUMBER_QTY_GT_ZERO");
  });

  it("NUMBER + VALIDITY + 列名含 PRICE → >= 0", () => {
    const r = suggestRuleExpression({
      column: { name: "UNIT_PRICE", dataType: "DECIMAL(18,2)" },
      ruleType: "VALIDITY",
    });
    expect(r.expression).toBe("UNIT_PRICE >= 0");
  });

  it("NUMBER + VALIDITY + 本体属性给 min/max → BETWEEN", () => {
    const r = suggestRuleExpression({
      column: { name: "AMOUNT", dataType: "NUMBER" },
      ruleType: "VALIDITY",
      ontologyProperty: { minValue: 0, maxValue: 999999 },
    });
    expect(r.expression).toBe("AMOUNT BETWEEN 0 AND 999999");
    expect(r.templateId).toBe("T_NUMBER_BETWEEN");
  });

  it("NUMBER + VALIDITY + 本体属性无 min/max → null", () => {
    const r = suggestRuleExpression({
      column: { name: "AMOUNT", dataType: "NUMBER" },
      ruleType: "VALIDITY",
    });
    expect(r.canAutoFill).toBe(false);
    expect(r.expression).toBeNull();
  });

  it("VARCHAR + VALIDITY + 列名含 NO → REGEX 模板", () => {
    const r = suggestRuleExpression({
      column: { name: "ORDER_NO", dataType: "VARCHAR2(32)" },
      ruleType: "VALIDITY",
    });
    expect(r.expression).toBe("ORDER_NO REGEX '^[A-Z0-9_-]+$'");
    expect(r.templateId).toBe("T_STRING_REGEX_CODE");
  });

  it("VARCHAR + UNIQUENESS → UNIQUE(<col>)，与生成器主键分支对齐", () => {
    const r = suggestRuleExpression({
      column: { name: "ORDER_NO", dataType: "VARCHAR2" },
      ruleType: "UNIQUENESS",
    });
    expect(r.canAutoFill).toBe(true);
    expect(r.expression).toBe("UNIQUE(ORDER_NO)");
    expect(r.templateId).toBe("T_UNIQUE_COLUMN");
  });

  it("VARCHAR + COMPLETENESS → COL IS NOT NULL（与生成器对齐）", () => {
    const r = suggestRuleExpression({
      column: { name: "REMARK", dataType: "VARCHAR2" },
      ruleType: "COMPLETENESS",
    });
    expect(r.canAutoFill).toBe(true);
    expect(r.expression).toBe("REMARK IS NOT NULL");
    expect(r.templateId).toBe("T_COMPLETENESS_NOT_NULL");
  });

  it("NUMBER + COMPLETENESS → COL IS NOT NULL", () => {
    const r = suggestRuleExpression({
      column: { name: "PO_QTY", dataType: "NUMBER" },
      ruleType: "COMPLETENESS",
    });
    expect(r.expression).toBe("PO_QTY IS NOT NULL");
    expect(r.templateId).toBe("T_COMPLETENESS_NOT_NULL");
  });

  it("DATE + VALIDITY + 列名含 START_DATE → 不晚于当前", () => {
    const r = suggestRuleExpression({
      column: { name: "START_DATE", dataType: "DATE" },
      ruleType: "VALIDITY",
    });
    expect(r.expression).toBe("START_DATE <= CURRENT_TIMESTAMP");
  });

  it("DATE + VALIDITY + 列名含 EXPIRE_DATE + 本体 min → >= min", () => {
    const r = suggestRuleExpression({
      column: { name: "EXPIRE_DATE", dataType: "DATE" },
      ruleType: "VALIDITY",
      ontologyProperty: { minValue: "2020-01-01" },
    });
    expect(r.expression).toBe("EXPIRE_DATE >= '2020-01-01'");
  });

  it("REFERENTIAL + 本体给 ref → REF <table>.<col>", () => {
    const r = suggestRuleExpression({
      column: { name: "SUPPLIER_KEY", dataType: "NUMBER" },
      ruleType: "REFERENTIAL",
      ontologyProperty: {
        isForeignKey: true,
        refSourceTable: "SUPPLIER",
        refSourceColumn: "SUPPLIER_KEY",
      },
    });
    expect(r.expression).toBe("REF SUPPLIER.SUPPLIER_KEY");
    expect(r.templateId).toBe("T_REF_TABLE_COL");
  });

  it("REFERENTIAL + 本体未给 ref → null（必须人工填）", () => {
    const r = suggestRuleExpression({
      column: { name: "SUPPLIER_KEY", dataType: "NUMBER" },
      ruleType: "REFERENTIAL",
    });
    expect(r.canAutoFill).toBe(false);
    expect(r.expression).toBeNull();
  });

  it("TIMELINESS → null（Phase 2 未实现）", () => {
    const r = suggestRuleExpression({
      column: { name: "ORDER_DATE", dataType: "DATE" },
      ruleType: "TIMELINESS",
    });
    expect(r.canAutoFill).toBe(false);
    expect(r.expression).toBeNull();
  });
});