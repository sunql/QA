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
  listReports,
  getReport,
  createReport,
  updateReport,
  deleteReport,
  regenerateReport,
  listSamples,
  compareReports,
  createShare,
  listShares,
  revokeShare,
  getPublicReport,
  getReportProgress,
} from "../api/evaluationReport";

describe("api/evaluationReport", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listReports GET /data-quality/reports（默认空 filter）", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
    const result = await listReports();
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/reports", {
      params: {},
    });
    expect(result).toEqual({ rows: [], total: 0 });
  });

  it("listReports 携带完整过滤参数（name/classId/ruleId/createdBy/date/分页）", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
    await listReports({
      name: "月度报告",
      classId: 7,
      ruleId: 12,
      createdBy: "alice",
      start: "2026-01-01",
      end: "2026-01-31",
      limit: 50,
      offset: 100,
    });
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/reports", {
      params: {
        name: "月度报告",
        classId: 7,
        ruleId: 12,
        createdBy: "alice",
        start: "2026-01-01",
        end: "2026-01-31",
        limit: 50,
        offset: 100,
      },
    });
  });

  it("listReports 剔除空串字段（name/createdBy 为空则不下发）", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
    await listReports({ name: "", createdBy: "" });
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/reports", {
      params: {},
    });
  });

  it("listReports 保留 0 值（classId/limit/offset 为 0 也下发）", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
    await listReports({ classId: 0, limit: 0, offset: 0 });
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/reports", {
      params: { classId: 0, limit: 0, offset: 0 },
    });
  });

  it("listReports(null) 归一为空参数", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
    await listReports(null as never);
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/reports", {
      params: {},
    });
  });

  it("getReport GET /data-quality/reports/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 3, name: "R3" } });
    const result = await getReport(3);
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/reports/3");
    expect(result.id).toBe(3);
  });

  it("createReport POST /data-quality/reports", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 9 } });
    const payload = {
      name: "新报告",
      classIds: [1, 2],
      ruleIds: [3],
      timeWindowStart: "2026-01-01T00:00:00Z",
      timeWindowEnd: "2026-01-31T00:00:00Z",
    };
    await createReport(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/data-quality/reports", payload);
  });

  it("updateReport PATCH /data-quality/reports/:id", async () => {
    httpMock.patch.mockResolvedValue({ data: { id: 4 } });
    await updateReport(4, { name: "改名", status: "PUBLISHED" } as never);
    expect(httpMock.patch).toHaveBeenCalledWith("/data-quality/reports/4", {
      name: "改名",
      status: "PUBLISHED",
    });
  });

  it("deleteReport DELETE /data-quality/reports/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: { id: 5, deletedAt: "now" } });
    const result = await deleteReport(5);
    expect(httpMock.delete).toHaveBeenCalledWith("/data-quality/reports/5");
    expect(result.deletedAt).toBe("now");
  });

  it("regenerateReport POST /data-quality/reports/:id/regenerate", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 6, snapshotVersion: 2 } });
    const result = await regenerateReport(6);
    expect(httpMock.post).toHaveBeenCalledWith(
      "/data-quality/reports/6/regenerate",
    );
    expect(result.snapshotVersion).toBe(2);
  });

  it("listSamples GET /data-quality/reports/:id/samples（默认空 params）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listSamples(7);
    expect(httpMock.get).toHaveBeenCalledWith(
      "/data-quality/reports/7/samples",
      { params: {} },
    );
  });

  it("listSamples GET /data-quality/reports/:id/samples（带 ruleId/limit）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listSamples(7, { ruleId: 42, limit: 10 });
    expect(httpMock.get).toHaveBeenCalledWith(
      "/data-quality/reports/7/samples",
      { params: { ruleId: 42, limit: 10 } },
    );
  });

  it("compareReports GET /data-quality/reports/:leftId/compare?otherId=", async () => {
    httpMock.get.mockResolvedValue({ data: { leftId: 1, rightId: 2 } });
    const result = await compareReports(1, 2);
    expect(httpMock.get).toHaveBeenCalledWith(
      "/data-quality/reports/1/compare",
      { params: { otherId: 2 } },
    );
    expect(result.rightId).toBe(2);
  });

  it("createShare POST /data-quality/reports/:reportId/share", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 11, shareToken: "tok" } });
    await createShare(8, { expiresInDays: 7 });
    expect(httpMock.post).toHaveBeenCalledWith(
      "/data-quality/reports/8/share",
      { expiresInDays: 7 },
    );
  });

  it("listShares GET /data-quality/reports/:reportId/share", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listShares(8);
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/reports/8/share");
  });

  it("revokeShare DELETE /data-quality/reports/shares/:shareId", async () => {
    httpMock.delete.mockResolvedValue({});
    await revokeShare(11);
    expect(httpMock.delete).toHaveBeenCalledWith(
      "/data-quality/reports/shares/11",
    );
  });

  it("getPublicReport GET /data-quality/reports/share/:token", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 8 } });
    await getPublicReport("abc123");
    expect(httpMock.get).toHaveBeenCalledWith(
      "/data-quality/reports/share/abc123",
    );
  });

  it("getReportProgress GET /data-quality/reports/:id/progress", async () => {
    httpMock.get.mockResolvedValue({ data: { stage: "RUNNING", completed: 1 } });
    const result = await getReportProgress(9);
    expect(httpMock.get).toHaveBeenCalledWith(
      "/data-quality/reports/9/progress",
    );
    expect(result.stage).toBe("RUNNING");
  });
});
