import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { getSupplier360 } from "../api/supplier";
import { getSupplierRisk } from "../api/supplierRisk";

describe("api/supplier + api/supplierRisk", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("getSupplier360 GET /supplier-360/:supplierKey", async () => {
    httpMock.get.mockResolvedValue({
      data: {
        profile: {
          enterpriseKey: 100,
          enterpriseCode: "ACME",
          owner: null,
          matchRule: null,
          effectiveDate: null,
          expiryDate: null,
        },
        entityCodes: [],
        kpis: [],
        fetchedAt: "2026-09-01T00:00:00Z",
      },
    });
    const result = await getSupplier360(100);
    expect(httpMock.get).toHaveBeenCalledWith("/supplier-360/100");
    expect(result.profile.enterpriseCode).toBe("ACME");
  });

  it("getSupplierRisk GET /supplier-risk/:supplierKey", async () => {
    httpMock.get.mockResolvedValue({
      data: {
        profile: {
          enterpriseKey: 200,
          enterpriseCode: "BETA",
          owner: null,
          matchRule: null,
          effectiveDate: null,
          expiryDate: null,
        },
        level: "medium" as const,
        levelSource: "risk_score",
        contributions: [],
        riskPoints: null,
        riskPointsSource: "fallback_template",
        recommendedActions: [],
        tokensUsed: 0,
        promptTokens: 0,
        completionTokens: 0,
        cost: 0,
        llmModelName: null,
        fetchedAt: "2026-09-01T00:00:00Z",
      },
    });
    const result = await getSupplierRisk(200);
    expect(httpMock.get).toHaveBeenCalledWith("/supplier-risk/200");
    expect(result.level).toBe("medium");
  });
});