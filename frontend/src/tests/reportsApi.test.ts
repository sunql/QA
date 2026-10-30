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
  listReportTemplates,
  generateReport,
  listReports,
  getReport,
  reviewReport,
} from "../api/reports";

describe("api/reports", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listReportTemplates GET /reports/templates", async () => {
    httpMock.get.mockResolvedValue({ data: [{ code: "TPL_A" }] });
    const result = await listReportTemplates();
    expect(httpMock.get).toHaveBeenCalledWith("/reports/templates");
    expect(result).toEqual([{ code: "TPL_A" }]);
  });

  it("generateReport POST /reports/generate", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 7 } });
    const input = { templateCode: "TPL_A", params: { orgId: "1" } };
    const result = await generateReport(input);
    expect(httpMock.post).toHaveBeenCalledWith("/reports/generate", input);
    expect(result).toEqual({ id: 7 });
  });

  it("listReports GET /reports（无 query ⇒ params 为 undefined）", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
    const result = await listReports();
    expect(httpMock.get).toHaveBeenCalledWith("/reports", { params: undefined });
    expect(result).toEqual({ rows: [], total: 0 });
  });

  it("listReports 携带 query 参数", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
    await listReports({ status: "PENDING_REVIEW", limit: 10, offset: 0 });
    expect(httpMock.get).toHaveBeenCalledWith("/reports", {
      params: { status: "PENDING_REVIEW", limit: 10, offset: 0 },
    });
  });

  it("getReport GET /reports/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 7, title: "T" } });
    const result = await getReport(7);
    expect(httpMock.get).toHaveBeenCalledWith("/reports/7");
    expect(result.id).toBe(7);
  });

  it("reviewReport POST /reports/:id/review（decision + note）", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 7, status: "APPROVED" } });
    const result = await reviewReport(7, { decision: "APPROVE", note: "ok" });
    expect(httpMock.post).toHaveBeenCalledWith("/reports/7/review", {
      decision: "APPROVE",
      note: "ok",
    });
    expect(result.status).toBe("APPROVED");
  });

  it("reviewReport note 为 null 时原样透传", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 7, status: "REJECTED" } });
    await reviewReport(7, { decision: "REJECT", note: null });
    expect(httpMock.post).toHaveBeenCalledWith("/reports/7/review", {
      decision: "REJECT",
      note: null,
    });
  });
});
