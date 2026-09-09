import { describe, it, expect, vi, beforeEach } from "vitest";

// mock httpClient（对齐 ontologyApi.test.ts / api.test.ts 的模式）
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

// mock axios（localImport.ts 的 executeImport 用独立 raw axios 实例）
const axiosPost = vi.hoisted(() => vi.fn());
vi.mock("axios", () => ({
  default: {
    create: () => ({ post: axiosPost }),
    __esModule: true,
  },
}));

import { getImportPreview, executeImport } from "../api/localImport";
import type {
  ImportExecuteRequest,
  ImportExecuteResponse,
  ImportPreviewRequest,
  ImportPreviewResponse,
  ImportRuleConfig,
} from "../types/localImport";

describe("api/localImport", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("getImportPreview POST /datasources/:id/import-preview（整请求体含 joinInference 与列选）", async () => {
    const rules: ImportRuleConfig = {
      tableFilter: {},
      typeMapping: {},
      joinInference: { inferDeclaredFk: true, inferNameConvention: false },
    };
    const data: ImportPreviewResponse = {
      datasourceId: 1,
      proposedClasses: [],
      proposedJoins: [],
      conflicts: [],
      filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
      llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
    };
    const request: ImportPreviewRequest = {
      rules,
      selectedTables: ["PORDER", "PORDERQ"],
      selectedColumns: { PORDERQ: ["POHNUM_0", "LIN_0"] },
    };
    httpMock.post.mockResolvedValue({ data });
    const result = await getImportPreview(1, request);
    expect(httpMock.post).toHaveBeenCalledWith(
      "/datasources/1/import-preview",
      request,
    );
    expect(result).toEqual(data);
  });

  it("executeImport 走独立 axios 实例并返回未解包的完整响应体（含 success 与 errors）", async () => {
    const request: ImportExecuteRequest = {
      confirmedClasses: [],
      confirmedJoins: [],
      conflictResolutions: [],
      syncEmbeddings: true,
    };
    const res: ImportExecuteResponse = {
      success: false,
      createdClasses: 0,
      createdProperties: 0,
      createdJoins: 0,
      skippedConflicts: 1,
      overwrittenConflicts: 0,
      errors: [{ type: "class", name: "Customer", message: "already exists" }],
    };
    axiosPost.mockResolvedValue({ data: res });
    const result = await executeImport(1, request);
    // 走独立 raw client，故 httpClient.post 不应被调用
    expect(httpMock.post).not.toHaveBeenCalled();
    expect(axiosPost).toHaveBeenCalledWith("/datasources/1/import", request);
    // 返回完整未解包响应体，success 与 errors 均保留
    expect(result).toEqual(res);
    expect(result.success).toBe(false);
    expect(result.errors).toHaveLength(1);
    expect(result.errors[0].message).toBe("already exists");
  });
});
