import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listGraphNodes,
  getGraphRelations,
  listEmbeddings,
  getVectorStats,
} from "../api/systemViewer";

// =============================================================================
// Neo4j
// =============================================================================

describe("api/systemViewer — Neo4j", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listGraphNodes GET /system/graph/nodes（默认 search=\"\"）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listGraphNodes("Class");
    expect(httpMock.get).toHaveBeenCalledWith("/system/graph/nodes", {
      params: { label: "Class", search: "" },
    });
  });

  it("listGraphNodes 携带 search 参数", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listGraphNodes("Property", "BPS");
    expect(httpMock.get).toHaveBeenCalledWith("/system/graph/nodes", {
      params: { label: "Property", search: "BPS" },
    });
  });

  it("getGraphRelations 用 unified_id 定位节点", async () => {
    // 节点主键是 unified_id（M0 起），形如 obj:CLASS:9 —— 冒号必须 encode，
    // 否则路径被 URL 解析截断。曾用数字 id，服务端恒返回空数组。
    httpMock.get.mockResolvedValue({
      data: [{ relType: "HAS_PROPERTY", targetUid: "obj:PROPERTY:10", targetName: "BPSNUM", targetLabel: "Property" }],
    });
    const result = await getGraphRelations("Class", "obj:CLASS:9");
    expect(httpMock.get).toHaveBeenCalledWith(
      "/system/graph/nodes/Class/obj%3ACLASS%3A9/relationships",
    );
    expect(result[0].relType).toBe("HAS_PROPERTY");
    expect(result[0].targetUid).toBe("obj:PROPERTY:10");
  });
});

// =============================================================================
// Milvus
// =============================================================================

describe("api/systemViewer — Milvus", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listEmbeddings GET /system/vectors/embeddings（无 type）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listEmbeddings();
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/system/vectors/embeddings");
    // type 为 undefined 时通过 || 转为 undefined
    expect(call[1].params).toEqual({ type: undefined, search: "" });
  });

  it("listEmbeddings 携带 type / search", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listEmbeddings("Metric", "po");
    expect(httpMock.get).toHaveBeenCalledWith("/system/vectors/embeddings", {
      params: { type: "Metric", search: "po" },
    });
  });

  it("getVectorStats GET /system/vectors/stats", async () => {
    httpMock.get.mockResolvedValue({ data: { class: 27, property: 350, metric: 80 } });
    const result = await getVectorStats();
    expect(httpMock.get).toHaveBeenCalledWith("/system/vectors/stats");
    expect(result.class).toBe(27);
  });
});