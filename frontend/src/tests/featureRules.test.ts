import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listFeatureRules,
  getFeatureRule,
  createFeatureRule,
  updateFeatureRule,
  deleteFeatureRule,
  toggleFeatureRule,
  parseFeatureRuleDescription,
} from "../api/featureRules";

describe("featureRules API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("listFeatureRules GET /feature-rules", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listFeatureRules();
    expect(httpMock.get).toHaveBeenCalledWith("/feature-rules", { params: {} });
  });

  it("listFeatureRules with enabledOnly=true", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listFeatureRules({ enabledOnly: true });
    expect(httpMock.get).toHaveBeenCalledWith("/feature-rules", {
      params: { enabledOnly: true },
    });
  });

  it("getFeatureRule GET /feature-rules/:code", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 1, code: "RULE01" } });
    await getFeatureRule("RULE01");
    expect(httpMock.get).toHaveBeenCalledWith("/feature-rules/RULE01");
  });

  it("createFeatureRule POST /feature-rules", async () => {
    const payload = {
      code: "RULE01",
      data_object: "SUPPLIER",
      data_layer: "DWD",
      target_level: "SUPPLIER",
      feature_name: "SUPPLIER_OTD",
      enabled: true,
      priority: 100,
      thresholds: [],
    };
    httpMock.post.mockResolvedValue({ data: { id: 1, ...payload } });
    await createFeatureRule(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/feature-rules", payload);
  });

  it("updateFeatureRule PUT /feature-rules/:code", async () => {
    const payload = { priority: 200, version: 1 };
    httpMock.put.mockResolvedValue({ data: { id: 1, code: "RULE01" } });
    await updateFeatureRule("RULE01", payload);
    expect(httpMock.put).toHaveBeenCalledWith("/feature-rules/RULE01", payload);
  });

  it("deleteFeatureRule DELETE /feature-rules/:code", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteFeatureRule("RULE01");
    expect(httpMock.delete).toHaveBeenCalledWith("/feature-rules/RULE01");
  });

  it("toggleFeatureRule POST /feature-rules/:code/toggle", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 1, enabled: false } });
    await toggleFeatureRule("RULE01", false);
    expect(httpMock.post).toHaveBeenCalledWith("/feature-rules/RULE01/toggle", {
      enabled: false,
    });
  });

  it("parseFeatureRuleDescription POST /feature-rules/parse-description", async () => {
    const payload = {
      data_object: "SUPPLIER",
      data_layer: "DWD",
      target_level: "SUPPLIER",
      natural_language: "当准时交付率低于 95% 时标记为高风险",
    };
    const response = {
      suggested_thresholds: [],
      reasoning: "test",
      overall_confidence: 0.9,
      warnings: [],
    };
    httpMock.post.mockResolvedValue({ data: response });
    await parseFeatureRuleDescription(payload);
    expect(httpMock.post).toHaveBeenCalledWith(
      "/feature-rules/parse-description",
      payload,
    );
  });
});
