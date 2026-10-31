import { describe, expect, it } from "vitest";
import { asReportSections } from "../utils/reportSections";

describe("asReportSections", () => {
  it("保留合法 text / chart 块", () => {
    const blocks = asReportSections([
      { kind: "text", content: "## 分析报告" },
      { kind: "chart", stepIndex: 0, title: "2024 销售额" },
    ]);

    expect(blocks).toEqual([
      { kind: "text", content: "## 分析报告" },
      { kind: "chart", stepIndex: 0, title: "2024 销售额" },
    ]);
  });

  it("丢弃形状不合法的块，保留其余（不冒未知形状进渲染层）", () => {
    const blocks = asReportSections([
      { kind: "text", content: "正文" },
      { kind: "text", content: 1 },
      { kind: "chart", stepIndex: "0" },
      { kind: "unknown", content: "x" },
      null,
    ]);

    expect(blocks).toEqual([{ kind: "text", content: "正文" }]);
  });

  it("非数组 / 空数组 / 全部非法 → null（走旧渲染）", () => {
    expect(asReportSections(undefined)).toBeNull();
    expect(asReportSections(null)).toBeNull();
    expect(asReportSections("text")).toBeNull();
    expect(asReportSections([])).toBeNull();
    expect(asReportSections([{ kind: "chart" }])).toBeNull();
  });
});
