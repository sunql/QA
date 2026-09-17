import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listRules,
  getRule,
  createRule,
  updateRule,
  disableRule,
} from "../api/dataQuality";

describe("api/dataQuality — rules", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listRules GET /data-quality/rules（无 params）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listRules();
    // params 展平后固定传 { params: flat }（无参 → 空对象）
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/rules", {
      params: {},
    });
  });

  it("listRules 携带 params", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listRules({ targetTable: "ERP.PORDER", enabled: true } as never);
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/data-quality/rules");
    expect(call[1].params).toEqual({ targetTable: "ERP.PORDER", enabled: true });
  });

  it("getRule GET /data-quality/rules/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 1, ruleName: "r1" } });
    const result = await getRule(1);
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/rules/1");
    expect(result.ruleName).toBe("r1");
  });

  it("createRule POST /data-quality/rules", async () => {
    const payload = {
      ruleName: "r1",
      ruleCode: "R001",
      datasourceId: 1,
      targetTable: "T",
      targetColumn: "C",
      ruleType: "COMPLETENESS" as const,
      severity: "HIGH" as const,
    };
    httpMock.post.mockResolvedValue({ data: { id: 5, ...payload } });
    await createRule(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/data-quality/rules", payload);
  });

  it("updateRule PUT /data-quality/rules/:id", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 1, isEnabled: false } });
    await updateRule(1, { isEnabled: false });
    expect(httpMock.put).toHaveBeenCalledWith("/data-quality/rules/1", { isEnabled: false });
  });

  it("disableRule DELETE /data-quality/rules/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: undefined });
    await disableRule(1);
    expect(httpMock.delete).toHaveBeenCalledWith("/data-quality/rules/1");
  });
});