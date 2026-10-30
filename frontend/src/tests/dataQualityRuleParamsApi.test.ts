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
  listRules,
  getRule,
  createRule,
  updateRule,
  deleteRule,
  fetchNextRuleCode,
} from "../api/dataQualityRuleParams";

describe("api/dataQualityRuleParams", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listRules GET /dq-rule-params/rules（不传 datasourceId → params 为 undefined）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listRules();
    expect(httpMock.get).toHaveBeenCalledWith("/dq-rule-params/rules", {
      params: undefined,
    });
  });

  it("listRules 携带 datasource_id（snake_case，对齐 FastAPI Query）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listRules(5);
    expect(httpMock.get).toHaveBeenCalledWith("/dq-rule-params/rules", {
      params: { datasource_id: 5 },
    });
  });

  it("getRule GET /dq-rule-params/rules/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 11, ruleCode: "R001" } });
    const result = await getRule(11);
    expect(httpMock.get).toHaveBeenCalledWith("/dq-rule-params/rules/11");
    expect(result.ruleCode).toBe("R001");
  });

  it("createRule POST /dq-rule-params/rules", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 12, ruleCode: "R002" } });
    const payload = {
      ruleCode: "R002",
      ruleName: "非空校验",
      ruleType: "COMPLETENESS" as const,
      targetTable: "ERP.PORDER",
      threshold: "0.9",
      severity: "HIGH",
      datasourceId: 5,
    };
    const result = await createRule(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/dq-rule-params/rules", payload);
    expect(result.id).toBe(12);
  });

  it("updateRule PUT /dq-rule-params/rules/:id", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 12, isEnabled: false } });
    await updateRule(12, { isEnabled: false } as never);
    expect(httpMock.put).toHaveBeenCalledWith("/dq-rule-params/rules/12", {
      isEnabled: false,
    });
  });

  it("deleteRule DELETE /dq-rule-params/rules/:id", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteRule(12);
    expect(httpMock.delete).toHaveBeenCalledWith("/dq-rule-params/rules/12");
  });

  it("fetchNextRuleCode GET /dq-rule-params/rules/next-code（无 date → params 为 undefined）", async () => {
    httpMock.get.mockResolvedValue({ data: { code: "DQ20260915-001", seq: 1 } });
    const result = await fetchNextRuleCode();
    expect(httpMock.get).toHaveBeenCalledWith("/dq-rule-params/rules/next-code", {
      params: undefined,
    });
    expect(result.seq).toBe(1);
  });

  it("fetchNextRuleCode 携带 date", async () => {
    httpMock.get.mockResolvedValue({ data: { code: "DQ20260101-001", seq: 7 } });
    await fetchNextRuleCode("20260101");
    expect(httpMock.get).toHaveBeenCalledWith("/dq-rule-params/rules/next-code", {
      params: { date: "20260101" },
    });
  });
});
