import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

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
