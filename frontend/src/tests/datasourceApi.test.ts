import { describe, it, expect, vi, beforeEach } from "vitest";

// httpClient 单独 mock；testDataSource 走独立 raw axios，需一并 mock。
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

const rawPost = vi.hoisted(() => vi.fn());

vi.mock("../api/client", () => ({ httpClient: httpMock }));
vi.mock("axios", () => ({
  default: {
    create: () => ({
      post: rawPost,
      interceptors: { request: { use: vi.fn() } },
    }),
  },
}));

import { LONG_RUNNING_TIMEOUT_MS } from "../config";
import {
  listDataSources,
  getDataSource,
  createDataSource,
  updateDataSource,
  deleteDataSource,
  listDatasourceSchemas,
  getDatasourceSchema,
  introspectDatasource,
  startIntrospectJob,
  getIntrospectJob,
  testDataSource,
} from "../api/datasource";

const schemaResponse = { tables: [], cachedAt: "2026-01-01T00:00:00Z" };

describe("api/datasource", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("listDataSources GET /datasources（activeOnly 透传）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listDataSources(true);
    expect(httpMock.get).toHaveBeenCalledWith("/datasources", { params: { activeOnly: true } });
  });

  it("getDataSource GET /datasources/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 1 } });
    await getDataSource(1);
    expect(httpMock.get).toHaveBeenCalledWith("/datasources/1");
  });

  it("createDataSource POST /datasources（payload 透传）", async () => {
    const payload = { name: "X" } as never;
    httpMock.post.mockResolvedValue({ data: payload });
    await createDataSource(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/datasources", payload);
  });

  it("updateDataSource PUT /datasources/:id（payload 透传）", async () => {
    const payload = { name: "Y" } as never;
    httpMock.put.mockResolvedValue({ data: payload });
    await updateDataSource(1, payload);
    expect(httpMock.put).toHaveBeenCalledWith("/datasources/1", payload);
  });

  it("deleteDataSource DELETE /datasources/:id", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteDataSource(1);
    expect(httpMock.delete).toHaveBeenCalledWith("/datasources/1");
  });

  it("listDatasourceSchemas GET /datasources/:id/schemas，返回 owner 数组", async () => {
    httpMock.get.mockResolvedValue({ data: ["THBI", "ZJTH"] });
    const owners = await listDatasourceSchemas(1);
    expect(httpMock.get).toHaveBeenCalledWith("/datasources/1/schemas");
    expect(owners).toEqual(["THBI", "ZJTH"]);
  });

  it("getDatasourceSchema 无 schema → GET 不带 params（连接默认 owner）", async () => {
    httpMock.get.mockResolvedValue({ data: schemaResponse });
    const res = await getDatasourceSchema(1);
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/datasources/1/schema");
    // 无 schema：不配置 params（后端走连接默认 owner）
    expect(call[1]?.params).toBeUndefined();
    expect(res).toEqual(schemaResponse);
  });

  it("getDatasourceSchema 带 schema → GET 携带 params.schema", async () => {
    httpMock.get.mockResolvedValue({ data: schemaResponse });
    await getDatasourceSchema(1, "THBI");
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/datasources/1/schema");
    expect(call[1].params).toEqual({ schema: "THBI" });
  });

  it("getDatasourceSchema schema 传 null → 按缺省处理，不带 params", async () => {
    httpMock.get.mockResolvedValue({ data: schemaResponse });
    await getDatasourceSchema(1, null);
    expect(httpMock.get.mock.calls[0][1]?.params).toBeUndefined();
  });

  it("introspectDatasource 无 schema → POST 不带 params（连接默认 owner）", async () => {
    httpMock.post.mockResolvedValue({ data: schemaResponse });
    const res = await introspectDatasource(1);
    const call = httpMock.post.mock.calls[0];
    expect(call[0]).toBe("/datasources/1/introspect");
    expect(call[1]).toBeUndefined();
    expect(call[2]?.params).toBeUndefined();
    expect(res).toEqual(schemaResponse);
  });

  it("introspectDatasource 带 schema → POST 携带 params.schema", async () => {
    httpMock.post.mockResolvedValue({ data: schemaResponse });
    await introspectDatasource(1, "THBI");
    const call = httpMock.post.mock.calls[0];
    expect(call[0]).toBe("/datasources/1/introspect");
    expect(call[2].params).toEqual({ schema: "THBI" });
  });

  it("introspectDatasource 必须用长超时 —— 全库内省 > 30s 会被掐断成「网络异常」", async () => {
    // 2026-11-25 本地实测：Oracle 大 owner（如 LC019999）全库内省超过默认
    // REQUEST_TIMEOUT_MS(30s) ⇒ axios status=0 ⇒ 前端误报「网络异常，请稍后重试」。
    // 与 ontology 批量重操作同口径，改用 LONG_RUNNING_TIMEOUT_MS(280s)。
    httpMock.post.mockResolvedValue({ data: schemaResponse });
    await introspectDatasource(1);
    const call = httpMock.post.mock.calls[0];
    expect(call[2]?.timeout).toBe(LONG_RUNNING_TIMEOUT_MS);

    // 带 schema 时 params 与 timeout 并存（不能因加 timeout 丢掉 params）
    httpMock.post.mockClear();
    await introspectDatasource(1, "LC019999");
    const call2 = httpMock.post.mock.calls[0];
    expect(call2[2].params).toEqual({ schema: "LC019999" });
    expect(call2[2].timeout).toBe(LONG_RUNNING_TIMEOUT_MS);
  });

  it("startIntrospectJob POST /datasources/:id/introspect-jobs（带 schema）", async () => {
    httpMock.post.mockResolvedValue({
      data: { jobId: "abc", status: "running", total: 0, completed: 0 },
    });
    const job = await startIntrospectJob(11, "LC019999");
    const call = httpMock.post.mock.calls[0];
    expect(call[0]).toBe("/datasources/11/introspect-jobs");
    expect(call[2].params).toEqual({ schema: "LC019999" });
    expect(job.jobId).toBe("abc");
  });

  it("getIntrospectJob GET /datasources/:id/introspect-jobs/:jobId", async () => {
    httpMock.get.mockResolvedValue({
      data: { jobId: "abc", status: "running", total: 860, completed: 200, error: null },
    });
    const job = await getIntrospectJob(11, "abc");
    expect(httpMock.get).toHaveBeenCalledWith("/datasources/11/introspect-jobs/abc", {
      timeout: expect.any(Number),
    });
    expect(job.completed).toBe(200);
    expect(job.total).toBe(860);
  });

  it("testDataSource 走独立 raw axios POST /datasources/test", async () => {
    const payload = { host: "h", port: 1521, username: "u", password: "p" } as never;
    rawPost.mockResolvedValue({ data: { success: true, message: "ok" } });
    const res = await testDataSource(payload);
    expect(rawPost).toHaveBeenCalledWith("/datasources/test", payload);
    expect(res).toEqual({ success: true, message: "ok" });
  });
});
