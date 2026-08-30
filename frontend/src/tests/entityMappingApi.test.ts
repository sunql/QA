import { describe, it, expect, vi, beforeEach } from "vitest";

// mock httpClient（对齐 ontologyApi.test.ts / localImportApi.test.ts 的模式）
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  createMapping,
  deleteMapping,
  getMapping,
  listMappings,
  updateMapping,
} from "../api/entityMapping";
import type { EntityMappingCreate, EntityMappingRead } from "../types/entityMapping";

const mapping: EntityMappingRead = {
  id: 1,
  entityType: "SUPPLIER",
  enterpriseKey: 100001,
  enterpriseCode: "SUP000001",
  sourceSystem: "ERP",
  sourceKey: "V000001",
  sourceCode: "V000001",
  matchRule: "MDM_MASTER",
  effectiveDate: "2026-01-01",
  expiryDate: "2099-12-31",
  owner: "procurement", // 服务端按 actor.departments[0] 派生，仅展示用
  createdTime: "2026-08-30T00:00:00Z",
  updatedTime: "2026-08-30T00:00:00Z",
};

describe("api/entityMapping", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("listMappings GET /entity-mappings 携带过滤参数", async () => {
    httpMock.get.mockResolvedValue({ data: [mapping] });
    const result = await listMappings({ entityType: "SUPPLIER" });
    expect(httpMock.get).toHaveBeenCalledWith("/entity-mappings", {
      params: { entityType: "SUPPLIER" },
    });
    expect(result).toEqual([mapping]);
  });

  it("listMappings 无过滤时不传 params", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    const result = await listMappings();
    expect(httpMock.get).toHaveBeenCalledWith("/entity-mappings", {
      params: undefined,
    });
    expect(result).toEqual([]);
  });

  it("getMapping GET /entity-mappings/:id", async () => {
    httpMock.get.mockResolvedValue({ data: mapping });
    const result = await getMapping(1);
    expect(httpMock.get).toHaveBeenCalledWith("/entity-mappings/1");
    expect(result).toEqual(mapping);
  });

  it("createMapping POST /entity-mappings 携带 camelCase payload", async () => {
    const payload: EntityMappingCreate = {
      entityType: "MATERIAL",
      enterpriseKey: 200001,
      enterpriseCode: "MAT000001",
      sourceSystem: "SRM",
      sourceKey: "M000001",
      sourceCode: "M000001",
    };
    httpMock.post.mockResolvedValue({ data: { ...mapping, id: 2 } });
    const result = await createMapping(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/entity-mappings", payload);
    expect(result.id).toBe(2);
  });

  it("updateMapping PUT /entity-mappings/:id 局部更新", async () => {
    // mock 返回已更新的实体（sourceCode 已变更为提交的值）
    httpMock.put.mockResolvedValue({
      data: { ...mapping, sourceCode: "V000001-X" },
    });
    const result = await updateMapping(1, { sourceCode: "V000001-X" });
    expect(httpMock.put).toHaveBeenCalledWith("/entity-mappings/1", {
      sourceCode: "V000001-X",
    });
    expect(result.sourceCode).toBe("V000001-X");
  });

  it("deleteMapping DELETE /entity-mappings/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: undefined });
    await deleteMapping(1);
    expect(httpMock.delete).toHaveBeenCalledWith("/entity-mappings/1");
  });
});
