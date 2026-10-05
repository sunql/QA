import { describe, it, expect } from "vitest";
import {
  asTablePayload,
  asVisualRationale,
} from "../utils/chartContract";

// Task 7（可视化输出策略）：两个新收窄函数的单元口径。
// 与 chartType 的严格白名单刻意不同：rationale 不内置 code 白名单（后端可先发新
// code，前端 i18n 缺 key 时显示 code 原文兜底），代价不对称 —— 图型白名单漏同步的
// 症状是「图静默消失」，rationale 漏一个 code 只是这行说明退化成英文码。

describe("asTablePayload — 明细表负载收窄", () => {
  it("columns/rows 都是数组才放行", () => {
    const payload = {
      columns: ["NAME", "QTY"],
      rows: [{ NAME: "A", QTY: 1 }],
      truncated: true,
    };
    expect(asTablePayload(payload)).toEqual({
      columns: ["NAME", "QTY"],
      rows: [{ NAME: "A", QTY: 1 }],
      truncated: true,
    });
  });

  it("rows 非数组 → null", () => {
    expect(asTablePayload({ columns: ["NAME"], rows: "not-an-array" })).toBeNull();
  });

  it("columns 非数组 → null", () => {
    expect(asTablePayload({ columns: "not-an-array", rows: [] })).toBeNull();
  });

  it("非普通对象（数组/标量/null）→ null", () => {
    expect(asTablePayload(null)).toBeNull();
    expect(asTablePayload(undefined)).toBeNull();
    expect(asTablePayload("table")).toBeNull();
    expect(asTablePayload([{ columns: [] }])).toBeNull();
  });

  it("truncated 只在命中行数上限时存在（absent 不归一成 false）", () => {
    const payload = { columns: ["NAME"], rows: [] };
    const result = asTablePayload(payload);
    expect(result).toEqual({ columns: ["NAME"], rows: [] });
    expect(result).not.toHaveProperty("truncated");
  });
});

describe("asVisualRationale — 判断依据收窄", () => {
  it("code 非空 string + params 普通对象才放行", () => {
    const rationale = { code: "R04_TOPN_HBAR", params: { rows: 5 } };
    expect(asVisualRationale(rationale)).toEqual({
      code: "R04_TOPN_HBAR",
      params: { rows: 5 },
    });
  });

  it("缺 code → null", () => {
    expect(asVisualRationale({ params: {} })).toBeNull();
  });

  it("code 为空串 → null", () => {
    expect(asVisualRationale({ code: "", params: {} })).toBeNull();
  });

  it("params 非普通对象 → null", () => {
    expect(asVisualRationale({ code: "R01_SINGLE_VALUE_KPI", params: null })).toBeNull();
    expect(asVisualRationale({ code: "R01_SINGLE_VALUE_KPI", params: "nope" })).toBeNull();
    expect(asVisualRationale({ code: "R01_SINGLE_VALUE_KPI", params: [1, 2] })).toBeNull();
  });

  it("未知 code 保留不吞（不内置白名单）", () => {
    expect(
      asVisualRationale({ code: "R99_FUTURE_RULE", params: { kind: "bar" } }),
    ).toEqual({ code: "R99_FUTURE_RULE", params: { kind: "bar" } });
  });

  it("非普通对象（数组/标量/null）→ null", () => {
    expect(asVisualRationale(null)).toBeNull();
    expect(asVisualRationale(undefined)).toBeNull();
    expect(asVisualRationale("rationale")).toBeNull();
    expect(asVisualRationale([{ code: "X" }])).toBeNull();
  });
});
