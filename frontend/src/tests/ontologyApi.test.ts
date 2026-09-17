import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
// parseBatchCsv 等裸 axios 路径的失败 toast 走 client.showMessageError（mock 成 spy 断言）
const showMessageErrorSpy = vi.hoisted(() => vi.fn());
vi.mock("../api/client", () => ({
  httpClient: httpMock,
  showMessageError: showMessageErrorSpy,
}));

// parseBatchCsv 必须走 axios.postForm（multipart 让浏览器补 boundary），
// 而非 httpClient（其实例默认 Content-Type: application/json 会把表单 422 掉）。
const axiosMock = vi.hoisted(() => ({ postForm: vi.fn() }));
vi.mock("axios", () => ({ default: axiosMock }));

// antd message.error spy：断言失败 toast（镜像拦截器文案）
const antdSpies = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return { ...actual, message: { ...actual.message, error: antdSpies.error } };
});

import {
  listClasses,
  listClassVersions,
  getClass,
  createClass,
  updateClass,
  deleteClass,
  listPropertiesByClass,
  getProperty,
  createProperty,
  updateProperty,
  deleteProperty,
  listMetrics,
  getMetric,
  createMetric,
  updateMetric,
  deleteMetric,
  listJoins,
  createJoin,
  deleteJoin,
  listSemanticRelations,
  createSemanticRelation,
  deleteSemanticRelation,
  backfillRelations,
  runOntologyBatch,
  previewOntologyBatch,
  downloadBatchTemplate,
  parseBatchCsv,
  syncClassEmbedding,
  syncMissingEmbeddings,
} from "../api/ontology";

// =============================================================================
// Class
// =============================================================================

describe("api/ontology — Class", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listClasses GET /ontology/classes（默认 includeExpired=false）", async () => {
    const data = [{ id: 1, className: "Customer" }];
    httpMock.get.mockResolvedValue({ data });
    const result = await listClasses();
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/classes", {
      params: { includeExpired: false },
    });
    expect(result).toEqual(data);
  });

  it("listClasses 支持 includeExpired=true", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listClasses({ includeExpired: true });
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/classes", {
      params: { includeExpired: true },
    });
  });

  it("listClassVersions GET /ontology/classes/:name/versions", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listClassVersions("Customer");
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/classes/Customer/versions");
  });

  it("getClass GET /ontology/classes/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 3, className: "Order" } });
    const result = await getClass(3);
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/classes/3");
    expect(result.className).toBe("Order");
  });

  it("createClass POST /ontology/classes", async () => {
    const payload = { className: "Product", classAlias: "商品" };
    httpMock.post.mockResolvedValue({ data: { id: 5, ...payload } });
    const result = await createClass(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/classes", payload);
    expect(result.id).toBe(5);
  });

  it("updateClass PUT /ontology/classes/:id", async () => {
    const payload = { classAlias: "客户" };
    httpMock.put.mockResolvedValue({ data: { id: 1, classAlias: "客户" } });
    const result = await updateClass(1, payload);
    expect(httpMock.put).toHaveBeenCalledWith("/ontology/classes/1", payload);
    expect(result.classAlias).toBe("客户");
  });

  it("deleteClass DELETE /ontology/classes/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteClass(7);
    expect(httpMock.delete).toHaveBeenCalledWith("/ontology/classes/7");
  });
});

// =============================================================================
// Property
// =============================================================================

describe("api/ontology — Property", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listPropertiesByClass GET /ontology/classes/:id/properties", async () => {
    const data = [{ id: 2, classId: 1, propertyName: "name" }];
    httpMock.get.mockResolvedValue({ data });
    const result = await listPropertiesByClass(1);
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/classes/1/properties");
    expect(result).toEqual(data);
  });

  it("getProperty GET /ontology/properties/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 8, propertyName: "qty" } });
    const result = await getProperty(8);
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/properties/8");
    expect(result.propertyName).toBe("qty");
  });

  it("createProperty POST /ontology/properties", async () => {
    const payload = { classId: 1, propertyName: "region_code", dataType: "STRING" };
    httpMock.post.mockResolvedValue({ data: { id: 11, ...payload } });
    const result = await createProperty(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/properties", payload);
    expect(result.id).toBe(11);
  });

  it("updateProperty PUT /ontology/properties/:id", async () => {
    const payload = { propertyAlias: "地区代码" };
    httpMock.put.mockResolvedValue({ data: { id: 4, propertyAlias: "地区代码" } });
    const result = await updateProperty(4, payload);
    expect(httpMock.put).toHaveBeenCalledWith("/ontology/properties/4", payload);
    expect(result.propertyAlias).toBe("地区代码");
  });

  it("deleteProperty DELETE /ontology/properties/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteProperty(9);
    expect(httpMock.delete).toHaveBeenCalledWith("/ontology/properties/9");
  });
});

// =============================================================================
// Metric
// =============================================================================

describe("api/ontology — Metric", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listMetrics GET /ontology/metrics", async () => {
    const data = [{ id: 1, metricName: "sales_amount" }];
    httpMock.get.mockResolvedValue({ data });
    const result = await listMetrics();
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/metrics");
    expect(result).toEqual(data);
  });

  it("getMetric GET /ontology/metrics/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 6, metricName: "order_cnt" } });
    const result = await getMetric(6);
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/metrics/6");
    expect(result.metricName).toBe("order_cnt");
  });

  it("createMetric POST /ontology/metrics", async () => {
    const payload = { metricName: "purchase_cnt", formula: "COUNT(id)", aggFunction: "COUNT" };
    httpMock.post.mockResolvedValue({ data: { id: 12, ...payload } });
    const result = await createMetric(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/metrics", payload);
    expect(result.id).toBe(12);
  });

  it("updateMetric PUT /ontology/metrics/:id", async () => {
    const payload = { metricAlias: "订单总量", aggFunction: "SUM" };
    httpMock.put.mockResolvedValue({ data: { id: 3, ...payload } });
    const result = await updateMetric(3, payload);
    expect(httpMock.put).toHaveBeenCalledWith("/ontology/metrics/3", payload);
    expect(result.aggFunction).toBe("SUM");
  });

  it("deleteMetric DELETE /ontology/metrics/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteMetric(10);
    expect(httpMock.delete).toHaveBeenCalledWith("/ontology/metrics/10");
  });
});

// =============================================================================
// Join
// =============================================================================

describe("api/ontology — Join", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listJoins GET /ontology/joins", async () => {
    const data = [{ id: 1, sourceClassId: 1, targetClassId: 2 }];
    httpMock.get.mockResolvedValue({ data });
    const result = await listJoins();
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/joins");
    expect(result).toEqual(data);
  });

  it("createJoin POST /ontology/joins", async () => {
    const payload = {
      sourceClassId: 1,
      sourceColumns: ["BPTNUM_0"],
      targetClassId: 2,
      targetColumns: ["BPRNUM_0"],
    };
    httpMock.post.mockResolvedValue({ data: { id: 9, ...payload } });
    const result = await createJoin(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/joins", payload);
    expect(result.id).toBe(9);
  });

  it("deleteJoin DELETE /ontology/joins/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteJoin(7);
    expect(httpMock.delete).toHaveBeenCalledWith("/ontology/joins/7");
  });
});

// =============================================================================
// Semantic Relation
// =============================================================================

describe("api/ontology — Semantic Relation", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listSemanticRelations GET /ontology/relations", async () => {
    const data = [{ id: 1, sourceClassId: 1, targetClassId: 2, relationType: "SUPPLIES" }];
    httpMock.get.mockResolvedValue({ data });
    const result = await listSemanticRelations();
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/relations");
    expect(result).toEqual(data);
  });

  it("createSemanticRelation POST /ontology/relations", async () => {
    const payload = {
      sourceClassId: 1,
      targetClassId: 2,
      relationType: "CONTAINS",
      description: "供应商供货",
    } as const;
    httpMock.post.mockResolvedValue({ data: { id: 5, ...payload } });
    const result = await createSemanticRelation(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/relations", payload);
    expect(result.id).toBe(5);
  });

  it("deleteSemanticRelation DELETE /ontology/relations/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteSemanticRelation(8);
    expect(httpMock.delete).toHaveBeenCalledWith("/ontology/relations/8");
  });

  it("backfillRelations POST /ontology/relations/backfill", async () => {
    const data = { syncedJoins: 3, backfilledReferences: 2 };
    httpMock.post.mockResolvedValue({ data });
    const result = await backfillRelations();
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/relations/backfill");
    expect(result).toEqual(data);
  });
});

// =============================================================================
// Batch Relation Engine
// =============================================================================

describe("api/ontology — Batch Relation Engine", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  const emptyResult = {
    syncGraph: null,
    inferredJoins: [],
    joins: { created: 0, skipped: 0, overwritten: 0, errors: [] },
    relations: { created: 0, skipped: 0, overwritten: 0, errors: [] },
  };

  it("runOntologyBatch POST /ontology/batch（携带动作 + onConflict）", async () => {
    const payload = { syncGraph: true, inferJoins: true, onConflict: "skip" as const };
    httpMock.post.mockResolvedValue({ data: emptyResult });
    const result = await runOntologyBatch(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/batch", payload);
    expect(result).toEqual(emptyResult);
  });

  it("runOntologyBatch 携带 applyManifest 清单", async () => {
    const payload = {
      applyManifest: true,
      onConflict: "overwrite" as const,
      manifest: {
        joins: [{ sourceClassId: 1, sourceColumns: ["A_0"], targetClassId: 2, targetColumns: ["A_0"] }],
        relations: [{ sourceClassId: 1, targetClassId: 2, relationType: "SUPPLIES" as const }],
      },
    };
    httpMock.post.mockResolvedValue({ data: emptyResult });
    await runOntologyBatch(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/batch", payload);
  });

  it("previewOntologyBatch POST /ontology/batch/preview（只读预览）", async () => {
    const payload = { inferJoins: true, onConflict: "skip" as const };
    httpMock.post.mockResolvedValue({ data: emptyResult });
    const result = await previewOntologyBatch(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/batch/preview", payload);
    expect(result).toEqual(emptyResult);
  });

  it("downloadBatchTemplate GET /ontology/batch/template?kind= 返回 blob", async () => {
    httpMock.get.mockResolvedValue({ data: new Blob(["a,b,c"], { type: "text/csv" }) });
    const blob = await downloadBatchTemplate("relations");
    expect(httpMock.get).toHaveBeenCalledWith("/ontology/batch/template", {
      params: { kind: "relations" },
      responseType: "blob",
    });
    expect(blob).toBeInstanceOf(Blob);
  });

  it("parseBatchCsv 走 axios.postForm（multipart 交浏览器补 boundary）+ auth 头，不走 httpClient JSON 通道", async () => {
    const file = new File(["a"], "relations.csv", { type: "text/csv" });
    const data = {
      manifest: { joins: [], relations: [{ sourceClassId: 1, targetClassId: 2, relationType: "SUPPLIES" }] },
      errors: [],
    };
    axiosMock.postForm.mockResolvedValue({ data });
    const result = await parseBatchCsv(file, "relations");
    // 回归护栏：httpClient 实例默认 Content-Type: application/json，
    // 若走它会丢 multipart boundary → 后端 422 missing file。因此必须不用它。
    expect(httpMock.post).not.toHaveBeenCalled();
    expect(axiosMock.postForm).toHaveBeenCalledTimes(1);
    const [url, formData, config] = axiosMock.postForm.mock.calls[0] as [
      string,
      FormData,
      { headers: Record<string, unknown> },
    ];
    expect(url).toMatch(/\/ontology\/batch\/parse-csv$/);
    expect(formData).toBeInstanceOf(FormData);
    expect(formData.get("kind")).toBe("relations");
    expect(formData.get("file")).toBe(file);
    expect(config.headers["X-Tenant-Id"]).toBeDefined();
    expect(config.headers["X-User-Id"]).toBeDefined();
    expect(config.headers["Content-Type"]).toBeUndefined();
    expect(result).toEqual(data);
  });

  it("parseBatchCsv 失败：showMessageError 走 React 上下文 messageApi 弹错后 rethrow", async () => {
    const file = new File(["a"], "relations.csv", { type: "text/csv" });
    // FastAPI 422 detail 形如数组
    const detail = [{ type: "missing", loc: ["body", "file"], msg: "Field required", input: null }];
    axiosMock.postForm.mockRejectedValue({ response: { status: 422, data: { detail } } });
    await expect(parseBatchCsv(file, "relations")).rejects.toBeTruthy();
    expect(showMessageErrorSpy).toHaveBeenCalledTimes(1);
  });
});

// =============================================================================
// Embedding 手动同步（向量对账）
// =============================================================================

describe("api/ontology — embedding 手动同步", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("syncClassEmbedding POST /ontology/classes/{id}/embedding", async () => {
    httpMock.post.mockResolvedValue({ data: null });
    await syncClassEmbedding(20);
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/classes/20/embedding");
  });

  it("syncMissingEmbeddings POST /ontology/embeddings/sync-missing 并返回摘要", async () => {
    const data = {
      totalClasses: 96,
      missingCount: 69,
      syncedCount: 69,
      failedCount: 0,
      failures: [],
    };
    httpMock.post.mockResolvedValue({ data });
    const result = await syncMissingEmbeddings();
    expect(httpMock.post).toHaveBeenCalledWith("/ontology/embeddings/sync-missing");
    expect(result).toEqual(data);
  });
});
