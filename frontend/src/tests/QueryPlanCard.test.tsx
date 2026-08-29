import { describe, it, expect } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import QueryPlanCard from "../components/chat/QueryPlanCard";
import type { QueryPlan } from "../types/chat";

function renderPlan(plan: QueryPlan) {
  return render(
    <ConfigProvider locale={zhCN}>
      <QueryPlanCard plan={plan} />
    </ConfigProvider>
  );
}

// 默认折叠；点击「查看查询计划」展开后再断言细节
function expandPanel() {
  fireEvent.click(screen.getByText("查看查询计划"));
}

describe("QueryPlanCard", () => {
  it("空 selectedClasses/selectedProperties 时显示 — 占位", () => {
    renderPlan({
      target: "t",
      selectedClasses: [],
      selectedProperties: [],
      aggregations: [],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    // 选中字段与涉及表均为空，FieldTags 渲染 —
    const emDashes = screen.getAllByText("—");
    expect(emDashes.length).toBeGreaterThanOrEqual(2);
  });

  it("interpretation 有值时展示「理解」区", () => {
    renderPlan({
      target: "各物料收货数量占比",
      interpretation: "统计每种物料的收货数量占全部收货数量的比例，术语「占比」= 比例",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    expect(screen.getByText("理解")).toBeInTheDocument();
    expect(
      screen.getByText("统计每种物料的收货数量占全部收货数量的比例，术语「占比」= 比例")
    ).toBeInTheDocument();
  });

  it("interpretation 为空时不展示「理解」区", () => {
    renderPlan({
      target: "t",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    expect(screen.queryByText("理解")).toBeNull();
  });

  it("target 为空字符串时显示 — 占位（而非空白）", () => {
    renderPlan({
      target: "",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    // target="" 时显示 —，而不是空白
    const targetRow = screen.getByText("查询目标");
    expect(targetRow.nextElementSibling?.textContent).toBe("—");
  });

  it("aggregation 含 alias 时展示 AS 子句", () => {
    renderPlan({
      target: "t",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [{ function: "SUM", property: "AMT", alias: "TOTAL" }],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    expect(screen.getByText("SUM(AMT) AS TOTAL")).toBeInTheDocument();
  });

  it("aggregation 无 alias 时不展示 AS 子句", () => {
    renderPlan({
      target: "t",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [{ function: "COUNT", property: "ID", alias: "" }],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    expect(screen.getByText("COUNT(ID)")).toBeInTheDocument();
    expect(screen.queryByText(/AS\s/)).toBeNull();
  });

  it("aggregation 含 formula 时优先展示公式而非 function(property)", () => {
    renderPlan({
      target: "各物料收货数量占比",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [
        {
          function: "SUM",
          property: "收货数量",
          alias: "占比",
          formula: "SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
        },
      ],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    // formula + alias 拼成完整表达式
    expect(
      screen.getByText("SUM(收货数量) / SUM(SUM(收货数量)) OVER () AS 占比")
    ).toBeInTheDocument();
    // 不应回退渲染 function(property)
    expect(screen.queryByText(/SUM\(收货数量\)$/)).toBeNull();
  });

  it("aggregation 含 formula 无 alias 时仅展示公式", () => {
    renderPlan({
      target: "占比",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [
        {
          function: "SUM",
          property: "收货数量",
          formula: "SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
        },
      ],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    expect(
      screen.getByText("SUM(收货数量) / SUM(SUM(收货数量)) OVER ()")
    ).toBeInTheDocument();
    expect(screen.queryByText(/AS\s/)).toBeNull();
  });

  it("groupBy / conditions 有值时展示对应面板", () => {
    renderPlan({
      target: "t",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [],
      groupBy: ["REGION", "PRODUCT"],
      conditions: ["STATUS = 'A'"],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    expect(screen.getByText("分组")).toBeInTheDocument();
    expect(screen.getByText("REGION")).toBeInTheDocument();
    expect(screen.getByText("PRODUCT")).toBeInTheDocument();
    expect(screen.getByText("筛选条件")).toBeInTheDocument();
    expect(screen.getByText("STATUS = 'A'")).toBeInTheDocument();
  });

  it("joins 有值时展示 ↔ 关系", () => {
    renderPlan({
      target: "t",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [],
      groupBy: [],
      conditions: [],
      joins: [
        { sourceClass: "ORDER", targetClass: "CUSTOMER", columns: [] },
        { sourceClass: "ORDER", targetClass: "PRODUCT", columns: [] },
      ],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    expect(screen.getByText("JOIN 关系")).toBeInTheDocument();
    expect(screen.getByText("ORDER ↔ CUSTOMER")).toBeInTheDocument();
    expect(screen.getByText("ORDER ↔ PRODUCT")).toBeInTheDocument();
  });

  it("rowLimit 非空时展示行数限制面板", () => {
    renderPlan({
      target: "t",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: 100,
    });
    expandPanel();
    expect(screen.getByText("行数限制")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
  });

  it("rowLimit=null 时不展示行数限制面板", () => {
    renderPlan({
      target: "t",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [],
      rowLimit: null,
    });
    expandPanel();
    expect(screen.queryByText("行数限制")).toBeNull();
  });

  it("sortBy 方向转大写展示", () => {
    renderPlan({
      target: "t",
      selectedClasses: ["T"],
      selectedProperties: ["C"],
      aggregations: [],
      groupBy: [],
      conditions: [],
      joins: [],
      sortBy: [
        { property: "AMT", direction: "asc" },
        { property: "DATE", direction: "desc" },
      ],
      rowLimit: null,
    });
    expandPanel();
    expect(screen.getByText("AMT ASC")).toBeInTheDocument();
    expect(screen.getByText("DATE DESC")).toBeInTheDocument();
  });
});