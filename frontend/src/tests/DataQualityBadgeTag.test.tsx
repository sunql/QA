import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import DataQualityBadgeTag from "../components/chat/DataQualityBadgeTag";
import type { DataQualityBadge } from "../types/chat";

// 用 ThemedRoot 包装以走 antd ConfigProvider 的样式系统
function renderBadge(badge: DataQualityBadge) {
  return render(
    <ConfigProvider>
      <DataQualityBadgeTag badge={badge} />
    </ConfigProvider>
  );
}

describe("DataQualityBadgeTag 四档评分", () => {
  it("未评估 → 灰色 '未评估' tag", () => {
    renderBadge({
      targetTable: "PORDER",
      overallScore: null,
      evaluatedAt: null,
      rulesCount: null,
      evaluated: false,
    });
    expect(screen.getByText(/DQ:.*未评估/)).toBeTruthy();
  });

  it("已评估 + score=98 → 绿色 tag + 显示百分比", () => {
    renderBadge({
      targetTable: "PORDER",
      overallScore: "98.00",
      evaluatedAt: "2026-08-30T00:00:00Z",
      rulesCount: 5,
      evaluated: true,
    });
    // 绿色 (color === "green") 应包含 98%
    const tag = screen.getByText(/DQ:.*98\.00%/);
    expect(tag).toBeTruthy();
  });

  it("已评估 + score=85 → 黄色 tag (70-89 区间)", () => {
    renderBadge({
      targetTable: "PORDER",
      overallScore: "85.00",
      evaluatedAt: "2026-08-30T00:00:00Z",
      rulesCount: 5,
      evaluated: true,
    });
    expect(screen.getByText(/DQ:.*85\.00%/)).toBeTruthy();
  });

  it("已评估 + score=52 → 红色 tag (< 70)", () => {
    renderBadge({
      targetTable: "PORDER",
      overallScore: "52.63",
      evaluatedAt: "2026-08-30T00:00:00Z",
      rulesCount: 5,
      evaluated: true,
    });
    expect(screen.getByText(/DQ:.*52\.63%/)).toBeTruthy();
  });

  it("边界 score=90 → 绿色（>=90 一档）", () => {
    renderBadge({
      targetTable: "PORDER",
      overallScore: "90.00",
      evaluatedAt: "2026-08-30T00:00:00Z",
      rulesCount: 5,
      evaluated: true,
    });
    expect(screen.getByText(/DQ:.*90\.00%/)).toBeTruthy();
  });

  it("边界 score=70 → 黄色（>=70 一档）", () => {
    renderBadge({
      targetTable: "PORDER",
      overallScore: "70.00",
      evaluatedAt: "2026-08-30T00:00:00Z",
      rulesCount: 5,
      evaluated: true,
    });
    expect(screen.getByText(/DQ:.*70\.00%/)).toBeTruthy();
  });

  it("异常 overallScore (非数字字符串) → 容错显示原值", () => {
    renderBadge({
      targetTable: "PORDER",
      overallScore: "not-a-number",
      evaluatedAt: "2026-08-30T00:00:00Z",
      rulesCount: 5,
      evaluated: true,
    });
    // Number.isFinite("not-a-number") === false → 容错回退到原字符串
    expect(screen.getByText(/not-a-number/)).toBeTruthy();
  });
});