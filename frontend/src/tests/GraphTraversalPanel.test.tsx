import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import GraphTraversalPanel from "../components/graph/GraphTraversalPanel";
import type { GraphTraversalRead } from "../types/graphTraversal";

const ok: GraphTraversalRead = {
  startType: "Supplier",
  startKey: "100001",
  maxHops: 3,
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
      toName: null,
      toType: "Material",
    },
  ],
  reachableTypes: ["Material"],
  fetchedAt: "2026-08-31T00:00:00Z",
};

vi.mock("../api/graphTraversal", () => ({
  traverseGraph: vi.fn(),
}));

import { traverseGraph } from "../api/graphTraversal";

const mockedTraverse = vi.mocked(traverseGraph);

describe("GraphTraversalPanel", () => {
  it("空 key 点击遍历 → 显示校验错误，不调用 API", async () => {
    mockedTraverse.mockReset();
    render(
      <ConfigProvider>
        <GraphTraversalPanel />
      </ConfigProvider>
    );
    await userEvent.click(screen.getByRole("button", { name: /遍历/ }));
    expect(await screen.findByText(/请输入实体键/)).toBeTruthy();
    expect(mockedTraverse).not.toHaveBeenCalled();
  });

  it("输入 key 点击遍历 → 调用 API 并渲染可达链", async () => {
    mockedTraverse.mockReset().mockResolvedValue(ok);
    render(
      <ConfigProvider>
        <GraphTraversalPanel />
      </ConfigProvider>
    );
    await userEvent.type(screen.getByPlaceholderText(/100001/), "100001");
    await userEvent.click(screen.getByRole("button", { name: /遍历/ }));

    await waitFor(() => expect(mockedTraverse).toHaveBeenCalledTimes(1));
    expect(mockedTraverse).toHaveBeenCalledWith("Supplier", "100001", 3);
    // 可达类型徽标 + 链路行
    expect(await screen.findByText("Material")).toBeTruthy();
    expect(screen.getByText("SUPPLIES")).toBeTruthy();
  });

  it("API 失败 → 显示错误消息", async () => {
    mockedTraverse.mockReset().mockRejectedValue(new Error("Neo4j down"));
    render(
      <ConfigProvider>
        <GraphTraversalPanel />
      </ConfigProvider>
    );
    await userEvent.type(screen.getByPlaceholderText(/100001/), "100001");
    await userEvent.click(screen.getByRole("button", { name: /遍历/ }));

    expect(await screen.findByText(/遍历失败/)).toBeTruthy();
  });
});
