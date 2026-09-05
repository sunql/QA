import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { listAuditLogs, getAuditLog } from "../api/audit";

describe("api/audit", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listAuditLogs GET /audit（默认空 filter）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listAuditLogs();
    expect(httpMock.get).toHaveBeenCalledWith("/audit", { params: {} });
  });

  it("listAuditLogs 携带 filters", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listAuditLogs({
      entityType: "Datasource",
      actor: "u1",
      limit: 20,
      offset: 0,
    } as never);
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/audit");
    expect(call[1].params).toEqual({
      entityType: "Datasource",
      actor: "u1",
      limit: 20,
      offset: 0,
    });
  });

  it("getAuditLog GET /audit/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 42, action: "CREATE" } });
    const result = await getAuditLog(42);
    expect(httpMock.get).toHaveBeenCalledWith("/audit/42");
    expect(result.action).toBe("CREATE");
  });
});