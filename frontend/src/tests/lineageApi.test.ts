import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listEdges,
  getEdge,
  createEdge,
  updateEdge,
  deleteEdge,
} from "../api/lineage";

describe("api/lineage — edges", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listEdges GET /lineage/edges（无 filter）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listEdges();
    expect(httpMock.get).toHaveBeenCalledWith("/lineage/edges", {
      params: undefined,
    });
  });

  it("listEdges 携带 filter", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listEdges({ sourceLayer: "ODS", isActive: true } as never);
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/lineage/edges");
    expect(call[1].params).toEqual({ sourceLayer: "ODS", isActive: true });
  });

  it("getEdge GET /lineage/edges/:id", async () => {
    httpMock.get.mockResolvedValue({
      data: { id: 1, sourceLayer: "ODS", targetLayer: "DWD" },
    });
    const result = await getEdge(1);
    expect(httpMock.get).toHaveBeenCalledWith("/lineage/edges/1");
    expect(result.targetLayer).toBe("DWD");
  });

  it("createEdge POST /lineage/edges", async () => {
    const payload = {
      sourceLayer: "SOURCE_SYSTEM" as const,
      sourceSystem: "ERP",
      sourceObject: "PORDER",
      sourceField: null,
      targetLayer: "ODS" as const,
      targetSystem: "DW",
      targetObject: "ODS_PORDER",
      targetField: null,
      transformationRule: null,
      refreshFrequency: "DAILY" as const,
      owner: null,
      description: null,
    };
    httpMock.post.mockResolvedValue({ data: { id: 99, ...payload } });
    await createEdge(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/lineage/edges", payload);
  });

  it("updateEdge PUT /lineage/edges/:id", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 1, isActive: false } });
    await updateEdge(1, { isActive: false });
    expect(httpMock.put).toHaveBeenCalledWith("/lineage/edges/1", { isActive: false });
  });

  it("deleteEdge DELETE /lineage/edges/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: undefined });
    await deleteEdge(1);
    expect(httpMock.delete).toHaveBeenCalledWith("/lineage/edges/1");
  });
});