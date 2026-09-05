import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listFeatures,
  getFeature,
  createFeature,
  updateFeature,
  deleteFeature,
  computeFeature,
  computeAllFeatures,
  listFeatureValues,
} from "../api/feature";

describe("api/feature", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listFeatures GET /features", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listFeatures();
    expect(httpMock.get).toHaveBeenCalledWith("/features");
  });

  it("getFeature GET /features/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 7 } });
    await getFeature(7);
    expect(httpMock.get).toHaveBeenCalledWith("/features/7");
  });

  it("createFeature POST /features", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 1 } });
    await createFeature({ name: "F1" } as never);
    expect(httpMock.post).toHaveBeenCalledWith("/features", { name: "F1" });
  });

  it("updateFeature PUT /features/:id", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 7 } });
    await updateFeature(7, { description: "x" } as never);
    expect(httpMock.put).toHaveBeenCalledWith("/features/7", { description: "x" });
  });

  it("deleteFeature DELETE /features/:id", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteFeature(9);
    expect(httpMock.delete).toHaveBeenCalledWith("/features/9");
  });

  it("computeFeature POST /features/:id/compute", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 7, value: 1 } });
    await computeFeature(7);
    expect(httpMock.post).toHaveBeenCalledWith("/features/7/compute");
  });

  it("computeAllFeatures POST /features/compute-batch", async () => {
    httpMock.post.mockResolvedValue({ data: { results: [] } });
    await computeAllFeatures();
    expect(httpMock.post).toHaveBeenCalledWith("/features/compute-batch");
  });

  it("listFeatureValues GET /features/:id/values", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listFeatureValues(7);
    expect(httpMock.get).toHaveBeenCalledWith("/features/7/values");
  });
});