import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listKpis,
  getKpi,
  createKpi,
  updateKpi,
  deleteKpi,
} from "../api/kpiCatalog";

describe("api/kpiCatalog", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listKpis GET /kpi-catalog", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listKpis();
    expect(httpMock.get).toHaveBeenCalledWith("/kpi-catalog");
  });

  it("getKpi GET /kpi-catalog/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 3 } });
    await getKpi(3);
    expect(httpMock.get).toHaveBeenCalledWith("/kpi-catalog/3");
  });

  it("createKpi POST /kpi-catalog", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 1 } });
    await createKpi({ name: "K1" } as never);
    expect(httpMock.post).toHaveBeenCalledWith("/kpi-catalog", { name: "K1" });
  });

  it("updateKpi PUT /kpi-catalog/:id", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 3 } });
    await updateKpi(3, { name: "新" } as never);
    expect(httpMock.put).toHaveBeenCalledWith("/kpi-catalog/3", { name: "新" });
  });

  it("deleteKpi DELETE /kpi-catalog/:id", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteKpi(3);
    expect(httpMock.delete).toHaveBeenCalledWith("/kpi-catalog/3");
  });
});