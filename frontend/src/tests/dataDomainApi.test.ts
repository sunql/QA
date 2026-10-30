import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listDataDomains,
  getDataDomain,
  createDataDomain,
  updateDataDomain,
  deleteDataDomain,
} from "../api/dataDomain";

describe("api/dataDomain", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listDataDomains GET /data-domains（默认 enabledOnly=false）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listDataDomains();
    expect(httpMock.get).toHaveBeenCalledWith("/data-domains", {
      params: { enabledOnly: false },
    });
  });

  it("listDataDomains(enabledOnly=true) 供下拉使用", async () => {
    httpMock.get.mockResolvedValue({ data: [{ code: "SALES" }] });
    const result = await listDataDomains(true);
    expect(httpMock.get).toHaveBeenCalledWith("/data-domains", {
      params: { enabledOnly: true },
    });
    expect(result).toEqual([{ code: "SALES" }]);
  });

  it("getDataDomain GET /data-domains/:code", async () => {
    httpMock.get.mockResolvedValue({ data: { code: "SALES", name: "销售" } });
    const result = await getDataDomain("SALES");
    expect(httpMock.get).toHaveBeenCalledWith("/data-domains/SALES");
    expect(result.code).toBe("SALES");
  });

  it("createDataDomain POST /data-domains", async () => {
    httpMock.post.mockResolvedValue({ data: { code: "SALES" } });
    const payload = { code: "SALES", name: "销售" };
    const result = await createDataDomain(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/data-domains", payload);
    expect(result.code).toBe("SALES");
  });

  it("updateDataDomain PUT /data-domains/:code", async () => {
    httpMock.put.mockResolvedValue({ data: { code: "SALES", name: "销售域" } });
    const payload = { name: "销售域", enabled: true };
    const result = await updateDataDomain("SALES", payload);
    expect(httpMock.put).toHaveBeenCalledWith("/data-domains/SALES", payload);
    expect(result.name).toBe("销售域");
  });

  it("deleteDataDomain DELETE /data-domains/:code", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteDataDomain("SALES");
    expect(httpMock.delete).toHaveBeenCalledWith("/data-domains/SALES");
  });
});
