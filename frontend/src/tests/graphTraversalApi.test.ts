import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { traverseGraph } from "../api/graphTraversal";
import type { GraphTraversalRead } from "../types/graphTraversal";

const TRAVERSAL: GraphTraversalRead = {
  startKey: "SUP-001",
  startType: "Supplier",
  maxHops: 2,
  hops: [
    {
      depth: 1,
      fromKey: "SUP-001",
      fromCode: "SUP-001",
      fromName: "供应商 A",
      fromType: "Supplier",
      relType: "SUPPLIES",
      toKey: "MAT-009",
      toCode: "MAT-009",
      toName: "物料 B",
      toType: "Material",
    },
  ],
  reachableTypes: ["Material"],
  fetchedAt: "2026-10-30T08:00:00Z",
};

describe("api/graphTraversal", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("traverseGraph GET /graph/traverse（startType/startKey/maxHops 进 query params）", async () => {
    httpMock.get.mockResolvedValue({ data: TRAVERSAL });
    const result = await traverseGraph("Supplier", "SUP-001", 2);
    expect(httpMock.get).toHaveBeenCalledWith("/graph/traverse", {
      params: { startType: "Supplier", startKey: "SUP-001", maxHops: 2 },
    });
    expect(result).toEqual(TRAVERSAL);
  });

  it("maxHops=0 也原样透传（不做静默兜底）", async () => {
    httpMock.get.mockResolvedValue({ data: TRAVERSAL });
    await traverseGraph("Material", "MAT-009", 0);
    expect(httpMock.get).toHaveBeenCalledWith("/graph/traverse", {
      params: { startType: "Material", startKey: "MAT-009", maxHops: 0 },
    });
  });

  it("请求失败时把后端错误原样抛出（不吞异常）", async () => {
    httpMock.get.mockRejectedValue(new Error("Request failed with status code 400"));
    await expect(traverseGraph("Supplier", "SUP-001", 2)).rejects.toThrow("400");
  });
});
