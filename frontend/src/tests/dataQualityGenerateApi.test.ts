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
  previewRules,
  confirmRules,
  listLlmModels,
  parseDescriptions,
  applySuggestion,
} from "../api/dataQualityGenerate";

const BASE = "/data-quality/rules/generate";

describe("api/dataQualityGenerate", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("previewRules POST /data-quality/rules/generate/preview", async () => {
    httpMock.post.mockResolvedValue({ data: { classId: 1, suggestions: [] } });
    const result = await previewRules(1, 2);
    expect(httpMock.post).toHaveBeenCalledWith(`${BASE}/preview`, {
      classId: 1,
      datasourceId: 2,
    });
    expect(result.classId).toBe(1);
  });

  it("confirmRules POST /data-quality/rules/generate/confirm", async () => {
    httpMock.post.mockResolvedValue({ data: { created: [], skippedCodes: ["R1"] } });
    const rules = [{ ruleCode: "R1" }] as never;
    const result = await confirmRules(2, rules);
    expect(httpMock.post).toHaveBeenCalledWith(`${BASE}/confirm`, {
      datasourceId: 2,
      rules,
    });
    expect(result.skippedCodes).toEqual(["R1"]);
  });

  it("listLlmModels GET /models?activeOnly=true（camelCase 参数名）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listLlmModels();
    expect(httpMock.get).toHaveBeenCalledWith("/models", {
      params: { activeOnly: true },
    });
  });

  it("listLlmModels 只保留 id / modelName / provider 三个字段", async () => {
    httpMock.get.mockResolvedValue({
      data: [
        {
          id: 3,
          modelName: "gpt-4o",
          provider: "OPENAI",
          // 后端多返回的字段（配置详情）必须被前端丢弃
          apiKey: "should-not-leak",
          temperature: 0.7,
        },
      ],
    });
    const result = await listLlmModels();
    expect(result).toEqual([{ id: 3, modelName: "gpt-4o", provider: "OPENAI" }]);
  });

  it("parseDescriptions POST /data-quality/rules/generate/parse-descriptions（modelId 缺省 ⇒ null）", async () => {
    httpMock.post.mockResolvedValue({ data: { suggestions: [], persistedPropertyIds: [] } });
    const result = await parseDescriptions(11);
    expect(httpMock.post).toHaveBeenCalledWith(`${BASE}/parse-descriptions`, {
      classId: 11,
      modelId: null,
    });
    expect(result.persistedPropertyIds).toEqual([]);
  });

  it("parseDescriptions 传 modelId 时原样带上", async () => {
    httpMock.post.mockResolvedValue({ data: { suggestions: [], persistedPropertyIds: [5] } });
    await parseDescriptions(11, 42);
    expect(httpMock.post).toHaveBeenCalledWith(`${BASE}/parse-descriptions`, {
      classId: 11,
      modelId: 42,
    });
  });

  it("applySuggestion 传 string[] ⇒ 兼容旧调用当 allowed_values", async () => {
    httpMock.post.mockResolvedValue({});
    await applySuggestion(9, ["A", "B"]);
    expect(httpMock.post).toHaveBeenCalledWith(`${BASE}/apply-suggestion`, {
      propertyId: 9,
      kind: "allowed_values",
      allowedValues: ["A", "B"],
    });
  });

  it("applySuggestion kind=allowed_values 缺省 allowedValues ⇒ 落空数组", async () => {
    httpMock.post.mockResolvedValue({});
    await applySuggestion(9, { kind: "allowed_values" });
    expect(httpMock.post).toHaveBeenCalledWith(`${BASE}/apply-suggestion`, {
      propertyId: 9,
      kind: "allowed_values",
      allowedValues: [],
    });
  });

  it("applySuggestion kind=not_null ⇒ 不带任何额外字段", async () => {
    httpMock.post.mockResolvedValue({});
    await applySuggestion(9, { kind: "not_null" });
    expect(httpMock.post).toHaveBeenCalledWith(`${BASE}/apply-suggestion`, {
      propertyId: 9,
      kind: "not_null",
    });
  });

  it("applySuggestion kind=range ⇒ 带 minValue + maxValue", async () => {
    httpMock.post.mockResolvedValue({});
    await applySuggestion(9, { kind: "range", minValue: "1", maxValue: "10" });
    expect(httpMock.post).toHaveBeenCalledWith(`${BASE}/apply-suggestion`, {
      propertyId: 9,
      kind: "range",
      minValue: "1",
      maxValue: "10",
    });
  });

  it("applySuggestion kind=pattern ⇒ 带 regexPattern", async () => {
    httpMock.post.mockResolvedValue({});
    await applySuggestion(9, { kind: "pattern", regexPattern: "^A.*" });
    expect(httpMock.post).toHaveBeenCalledWith(`${BASE}/apply-suggestion`, {
      propertyId: 9,
      kind: "pattern",
      regexPattern: "^A.*",
    });
  });
});
