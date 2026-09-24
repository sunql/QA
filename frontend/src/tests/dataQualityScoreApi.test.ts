import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  evaluateRule,
  evaluateBatch,
  computeScore,
  listScores,
} from "../api/dataQualityScore";

describe("api/dataQualityScore", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("evaluateRule POST /data-quality/rules/:id/evaluate", async () => {
    httpMock.post.mockResolvedValue({
      data: { ruleId: 1, status: "PASSED", evaluatedRows: 100, failedRows: 0 },
    });
    const result = await evaluateRule(1);
    expect(httpMock.post).toHaveBeenCalledWith("/data-quality/rules/1/evaluate");
    expect(result.status).toBe("PASSED");
  });

  it("evaluateBatch POST /data-quality/rules/evaluate-batch", async () => {
    const payload = { ruleIds: [1, 2, 3] };
    httpMock.post.mockResolvedValue({ data: { results: [], totalRules: 3 } });
    await evaluateBatch(payload);
    expect(httpMock.post).toHaveBeenCalledWith(
      "/data-quality/rules/evaluate-batch",
      payload,
    );
  });

  it("computeScore POST /data-quality/scores/compute", async () => {
    httpMock.post.mockResolvedValue({ data: { computedAt: "2026-09-02T10:00:00Z" } });
    await computeScore();
    expect(httpMock.post).toHaveBeenCalledWith("/data-quality/scores/compute", {});
  });

  it("listScores GET /data-quality/scores（无 filters）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listScores();
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/scores", {
      params: undefined,
    });
  });

  it("listScores 携带 filters", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listScores({ targetTable: "ERP.PORDER", minScore: 80 } as never);
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/data-quality/scores");
    expect(call[1].params).toEqual({ targetTable: "ERP.PORDER", minScore: 80 });
  });
});