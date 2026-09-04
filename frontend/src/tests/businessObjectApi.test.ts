import { describe, it, expect, vi, beforeEach } from "vitest";

// mock httpClient — hoisted + vi.mock pattern (matching entityMappingApi.test.ts)
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listBusinessObjects,
  getBusinessObject,
  createBusinessObject,
  updateBusinessObject,
  deleteBusinessObject,
} from "../api/businessObject";

describe("api/businessObject", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("listBusinessObjects uses correct path", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listBusinessObjects();
    expect(httpMock.get).toHaveBeenCalledWith("/business-objects");
  });

  it("getBusinessObject uses path with code", async () => {
    httpMock.get.mockResolvedValue({ data: {} });
    await getBusinessObject("SUPPLIER");
    expect(httpMock.get).toHaveBeenCalledWith("/business-objects/SUPPLIER");
  });

  it("createBusinessObject sends camelCase payload", async () => {
    httpMock.post.mockResolvedValue({ data: {} });
    await createBusinessObject({
      code: "SUPPLIER",
      name: "供应商",
      graphLabel: "Supplier",
    });
    expect(httpMock.post).toHaveBeenCalledWith("/business-objects", {
      code: "SUPPLIER",
      name: "供应商",
      graphLabel: "Supplier",
    });
  });

  it("updateBusinessObject uses PUT with code", async () => {
    httpMock.put.mockResolvedValue({ data: {} });
    await updateBusinessObject("SUPPLIER", { name: "新名字" });
    expect(httpMock.put).toHaveBeenCalledWith("/business-objects/SUPPLIER", {
      name: "新名字",
    });
  });

  it("deleteBusinessObject uses DELETE with code", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteBusinessObject("NCR");
    expect(httpMock.delete).toHaveBeenCalledWith("/business-objects/NCR");
  });
});
