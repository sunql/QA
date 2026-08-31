import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import GraphTraversalCard from "../components/chat/GraphTraversalCard";
import type { GraphTraversalRead } from "../types/graphTraversal";

const baseTraversal: GraphTraversalRead = {
  startType: "Supplier",
  startKey: "100001",
  maxHops: 2,
  hops: [
    {
      depth: 1,
      fromKey: "100001",
      fromCode: "SUP000001",
      fromName: "钢铁供应商",
      fromType: "Supplier",
      relType: "SUPPLIES",
      toKey: "200001",
      toCode: "RM-STEEL-001",
      toName: "钢材",
      toType: "Material",
    },
  ],
  reachableTypes: ["Material", "Contract"],
  fetchedAt: "2026-08-31T00:00:00Z",
};

describe("GraphTraversalCard", () => {
  it("渲染起点类型 / key / 跳数与可达类型徽标", () => {
    render(
      <ConfigProvider>
        <GraphTraversalCard data={baseTraversal} />
      </ConfigProvider>
    );
    // 头部信息
    expect(screen.getByText("Supplier")).toBeTruthy();
    expect(screen.getByText("100001")).toBeTruthy();
    // 可达类型徽标
    expect(screen.getByText("Material")).toBeTruthy();
    expect(screen.getByText("Contract")).toBeTruthy();
  });

  it("渲染逐跳链路（from → to + 关系）", () => {
    render(
      <ConfigProvider>
        <GraphTraversalCard data={baseTraversal} />
      </ConfigProvider>
    );
    expect(screen.getByText("SUPPLIES")).toBeTruthy();
    // from 列：Supplier · SUP000001；to 列：Material · RM-STEEL-001
    expect(screen.getByText(/Supplier · SUP000001/)).toBeTruthy();
    expect(screen.getByText(/Material · RM-STEEL-001/)).toBeTruthy();
  });

  it("空 hops → 显示空结果提示（非 404）", () => {
    const empty: GraphTraversalRead = {
      ...baseTraversal,
      hops: [],
      reachableTypes: [],
    };
    render(
      <ConfigProvider>
        <GraphTraversalCard data={empty} />
      </ConfigProvider>
    );
    expect(screen.getByText(/无关联业务实体/)).toBeTruthy();
  });

  it("多跳行渲染真实末边（from=中间节点, rel=末边, to=终点）", () => {
    const multi: GraphTraversalRead = {
      ...baseTraversal,
      hops: [
        {
          depth: 1,
          fromKey: "100001",
          fromCode: "SUP000001",
          fromName: null,
          fromType: "Supplier",
          relType: "SUPPLIES",
          toKey: "200001",
          toCode: "RM-STEEL-001",
          toName: "钢材",
          toType: "Material",
        },
        {
          depth: 2,
          fromKey: "200001",
          fromCode: "RM-STEEL-001",
          fromName: "钢材",
          fromType: "Material",
          relType: "CONTAINS",
          toKey: "300001",
          toCode: "PO202608001",
          toName: null,
          toType: "PurchaseOrder",
        },
      ],
      reachableTypes: ["Material", "PurchaseOrder"],
    };
    render(
      <ConfigProvider>
        <GraphTraversalCard data={multi} />
      </ConfigProvider>
    );
    // 深度 2 行是真实边 Material → CONTAINS → PurchaseOrder（非起点折叠 Supplier→PO）
    expect(screen.getByText("CONTAINS")).toBeTruthy();
    expect(screen.getAllByText(/Material · RM-STEEL-001/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/PurchaseOrder · PO202608001/)).toBeTruthy();
  });
});
