import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import QueryPlanCard from "../components/chat/QueryPlanCard";
import type { DataQualityBadge, QueryPlan } from "../types/chat";

const basePlan: QueryPlan = {
  target: "测试查询",
  selectedClasses: ["PORDER", "BPSUPPLIER"],
  selectedProperties: ["PONUM"],
  conditions: [],
  aggregations: [],
  groupBy: [],
  joins: [],
  sortBy: [],
  rowLimit: null,
  interpretation: null,
};

async function renderCardExpanded(plan: QueryPlan, dataQuality?: DataQualityBadge[] | null) {
  const view = render(
    <ConfigProvider>
      <QueryPlanCard plan={plan} dataQuality={dataQuality} />
    </ConfigProvider>
  );
  // antd Collapse 默认收起，点击面板头展开以渲染 children
  await userEvent.click(screen.getByText("查看查询计划"));
  return view;
}

describe("QueryPlanCard × DataQualityBadge", () => {
  it("未传 dataQuality → 不渲染 badge 区（向后兼容）", async () => {
    const { container } = await renderCardExpanded(basePlan, undefined);
    expect(container.textContent).not.toMatch(/DQ:/);
  });

  it("dataQuality 空数组 → 不渲染 badge 区", async () => {
    const { container } = await renderCardExpanded(basePlan, []);
    expect(container.textContent).not.toMatch(/DQ:/);
  });

  it("dataQuality null → 不渲染 badge 区", async () => {
    const { container } = await renderCardExpanded(basePlan, null);
    expect(container.textContent).not.toMatch(/DQ:/);
  });

  it("多张表 + 各自 badge → 渲染对应数量", async () => {
    const badges: DataQualityBadge[] = [
      {
        targetTable: "PORDER",
        overallScore: "98.00",
        evaluatedAt: "2026-08-30T00:00:00Z",
        rulesCount: 5,
        evaluated: true,
      },
      {
        targetTable: "BPSUPPLIER",
        overallScore: null,
        evaluatedAt: null,
        rulesCount: null,
        evaluated: false,
      },
    ];
    const { container } = await renderCardExpanded(basePlan, badges);
    expect(container.textContent).toMatch(/98\.00%/);
    expect(container.textContent).toMatch(/DQ:.*未评估/);
  });

  it("selectedClasses 与 dataQuality 数量不一致 → 只渲染有 badge 的表", async () => {
    const badges: DataQualityBadge[] = [
      {
        targetTable: "PORDER",
        overallScore: "85.00",
        evaluatedAt: "2026-08-30T00:00:00Z",
        rulesCount: 5,
        evaluated: true,
      },
      // BPSUPPLIER 无 badge
    ];
    const { container } = await renderCardExpanded(basePlan, badges);
    // 只有 1 个 DQ tag（count occurrences of "DQ:" literal）
    const matches = container.textContent?.match(/DQ:/g) ?? [];
    expect(matches).toHaveLength(1);
  });
});