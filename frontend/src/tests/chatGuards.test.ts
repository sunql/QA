import { describe, it, expect } from "vitest";
import { isStepPlan, isStepResult } from "../api/chat";

describe("isStepPlan — 运行时收窄", () => {
  it("null/undefined → false", () => {
    expect(isStepPlan(null)).toBe(false);
    expect(isStepPlan(undefined)).toBe(false);
  });

  it("非对象 → false", () => {
    expect(isStepPlan("string")).toBe(false);
    expect(isStepPlan(123)).toBe(false);
  });

  it("缺 stepIndex → false", () => {
    expect(isStepPlan({ description: "x", subQuestion: "y" })).toBe(false);
  });

  it("stepIndex 非合法值 → false", () => {
    expect(isStepPlan({ stepIndex: "bad", description: "x", subQuestion: "y" })).toBe(false);
  });

  it("缺 description → false", () => {
    expect(isStepPlan({ stepIndex: 1, subQuestion: "y" })).toBe(false);
  });

  it("缺 subQuestion → false", () => {
    expect(isStepPlan({ stepIndex: 1, description: "x" })).toBe(false);
  });

  it("合法 stepIndex（正整数） → true", () => {
    expect(
      isStepPlan({ stepIndex: 1, description: "x", subQuestion: "y" }),
    ).toBe(true);
  });

  it("合法 stepIndex = 0 → true（>= 0 即合法）", () => {
    expect(isStepPlan({ stepIndex: 0, description: "x", subQuestion: "y" })).toBe(true);
  });

  it("非法 stepIndex（负数）→ false", () => {
    expect(isStepPlan({ stepIndex: -1, description: "x", subQuestion: "y" })).toBe(false);
  });
});

describe("isStepResult — 复用 isStepPlan", () => {
  it("非法负载 → false", () => {
    expect(isStepResult(null)).toBe(false);
    expect(isStepResult({})).toBe(false);
  });

  it("合法负载 → true（与 isStepPlan 共享逻辑）", () => {
    expect(
      isStepResult({ stepIndex: 5, description: "x", subQuestion: "y" }),
    ).toBe(true);
  });
});