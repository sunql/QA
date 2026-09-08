import { describe, it, expect, vi, beforeEach } from "vitest";

// mock httpClient 与 axios（datasource.ts 用独立 raw axios 实例做连接测试）
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

const axiosPost = vi.hoisted(() => vi.fn());
vi.mock("axios", () => ({
  default: {
    create: () => ({ post: axiosPost }),
    __esModule: true,
  },
}));

import {
  listModels,
  getModel,
  createModel,
  updateModel,
  deactivateModel,
} from "../api/modelConfig";
import {
  listSessions,
  getGlobalUsageSummary,
  getDailyUsageTrends,
  getModelUsage,
  getSessionUsageSummary,
  listSessionUsage,
} from "../api/session";
import {
  listDataSources,
  getDataSource,
  createDataSource,
  updateDataSource,
  deleteDataSource,
  testDataSource,
} from "../api/datasource";
import type { ModelConfig, ModelConfigCreate, ModelConfigUpdate } from "../types/modelConfig";
import type {
  DataSource,
  DataSourceCreate,
  DataSourceTestRequest,
  DataSourceTestResponse,
  DataSourceUpdate,
} from "../types/datasource";

describe("api/modelConfig", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("listModels 传递 activeOnly 参数并返回列表", async () => {
    const data = [{ id: 1 }] as ModelConfig[];
    httpMock.get.mockResolvedValue({ data });
    const result = await listModels(true);
    // 关键：query 参数名是 camelCase（与 FastAPI kwarg 名一致），
    // 写 snake_case 会被 FastAPI 静默忽略，过滤永远不生效。
    expect(httpMock.get).toHaveBeenCalledWith("/models", { params: { activeOnly: true } });
    expect(result).toEqual(data);
  });

  it("getModel 按 id 查询单个模型", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 5 } });
    const result = await getModel(5);
    expect(httpMock.get).toHaveBeenCalledWith("/models/5");
    expect(result.id).toBe(5);
  });

  it("createModel 发起 POST 并返回新模型", async () => {
    const payload: ModelConfigCreate = {
      modelName: "gpt-4o",
      provider: "OPENAI",
      apiEndpoint: "https://api.openai.com/v1",
      apiKey: "sk-x",
      costPer1KInput: 0.005,
      costPer1KOutput: 0.015,
      maxInputTokens: 128000,
      weight: 1,
      costThreshold: 2,
    };
    httpMock.post.mockResolvedValue({ data: { id: 9, ...payload } });
    const result = await createModel(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/models", payload);
    expect(result.id).toBe(9);
  });

  it("updateModel 发起 PUT 并返回更新后模型", async () => {
    const payload: ModelConfigUpdate = { weight: 3, isActive: false };
    httpMock.put.mockResolvedValue({ data: { id: 2 } });
    const result = await updateModel(2, payload);
    expect(httpMock.put).toHaveBeenCalledWith("/models/2", payload);
    expect(result.id).toBe(2);
  });

  it("deactivateModel 发起 DELETE", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deactivateModel(7);
    expect(httpMock.delete).toHaveBeenCalledWith("/models/7");
  });
});

describe("api/datasource", () => {
  const sample: DataSource = {
    id: 1,
    name: "ZJTH-Oracle",
    type: "oracle",
    host: "10.0.0.1",
    port: 1521,
    databaseName: "orcl",
    username: "u",
    isActive: true,
    isDefault: false,
    oracleVersion: null,
    description: null,
    createdBy: null,
    createdTime: "2026-01-01T00:00:00Z",
    updatedTime: "2026-01-01T00:00:00Z",
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("listDataSources 传 activeOnly 参数并返回列表", async () => {
    httpMock.get.mockResolvedValue({ data: [sample] });
    const result = await listDataSources(true);
    expect(httpMock.get).toHaveBeenCalledWith("/datasources", { params: { activeOnly: true } });
    expect(result).toEqual([sample]);
  });

  it("listDataSources 默认 activeOnly=false", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listDataSources();
    expect(httpMock.get).toHaveBeenCalledWith("/datasources", { params: { activeOnly: false } });
  });

  it("getDataSource 按 id 查询单个数据源", async () => {
    httpMock.get.mockResolvedValue({ data: sample });
    const result = await getDataSource(1);
    expect(httpMock.get).toHaveBeenCalledWith("/datasources/1");
    expect(result.id).toBe(1);
  });

  it("createDataSource 发起 POST 并返回新建记录", async () => {
    const payload: DataSourceCreate = {
      name: "RuoYi-MySQL",
      type: "mysql",
      host: "127.0.0.1",
      port: 3306,
      databaseName: "ry-vue",
      username: "root",
      password: "pwd",
      isActive: true,
      isDefault: false,
      oracleVersion: null,
    };
    httpMock.post.mockResolvedValue({ data: { id: 2, ...payload } });
    const result = await createDataSource(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/datasources", payload);
    expect(result.id).toBe(2);
  });

  it("updateDataSource 发起 PUT 并返回更新后记录", async () => {
    const payload: DataSourceUpdate = { isDefault: true };
    httpMock.put.mockResolvedValue({ data: { ...sample, isDefault: true } });
    const result = await updateDataSource(1, payload);
    expect(httpMock.put).toHaveBeenCalledWith("/datasources/1", payload);
    expect(result.isDefault).toBe(true);
  });

  it("deleteDataSource 发起 DELETE", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteDataSource(3);
    expect(httpMock.delete).toHaveBeenCalledWith("/datasources/3");
  });

  it("testDataSource 走独立 axios 实例（不走 httpClient），success=false 也能取到 message", async () => {
    const req: DataSourceTestRequest = {
      type: "oracle",
      host: "10.0.0.1",
      port: 1521,
      databaseName: "orcl",
      username: "u",
      password: "pwd",
    };
    const res: DataSourceTestResponse = { success: false, message: "ORA-01017: invalid credentials" };
    axiosPost.mockResolvedValue({ data: res });
    const result = await testDataSource(req);
    // 走独立实例，故 httpClient.post 不应被调用
    expect(httpClient_unused_call_count()).toBe(0);
    expect(axiosPost).toHaveBeenCalledWith("/datasources/test", req);
    expect(result.success).toBe(false);
    expect(result.message).toContain("ORA-01017");
  });
});

// 统计 httpClient 在 testDataSource 用例期间的调用次数（验证走独立 axios）
function httpClient_unused_call_count(): number {
  return httpMock.post.mock.calls.length + httpMock.get.mock.calls.length;
}

describe("api/session", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("listSessions 查询会话列表", async () => {
    const list = [{ sessionId: "s1", totalRequests: 1, totalTokens: 10, totalCost: 0.001, firstRequestTime: "2026-08-12T00:00:00Z", lastRequestTime: "2026-08-12T00:00:00Z", lastQuestion: "q" }];
    httpMock.get.mockResolvedValue({ data: list });
    const result = await listSessions();
    expect(httpMock.get).toHaveBeenCalledWith("/sessions");
    expect(result).toHaveLength(1);
  });

  it("getGlobalUsageSummary 查询全局汇总", async () => {
    const summary = { totalSessions: 3, totalRequests: 7, totalTokens: 250, totalCost: 0.012 };
    httpMock.get.mockResolvedValue({ data: summary });
    const result = await getGlobalUsageSummary();
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/usage/global");
    expect(result.totalSessions).toBe(3);
  });

  it("getDailyUsageTrends 查询每日趋势", async () => {
    const trends = [{ date: "2026-08-12", requests: 2, tokens: 80, cost: 0.003 }];
    httpMock.get.mockResolvedValue({ data: trends });
    const result = await getDailyUsageTrends();
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/usage/daily");
    expect(result[0].date).toBe("2026-08-12");
  });

  it("getModelUsage 查询模型维度聚合", async () => {
    const models = [{ modelName: "deepseek-chat", requests: 2, tokens: 180, cost: 0.007 }];
    httpMock.get.mockResolvedValue({ data: models });
    const result = await getModelUsage();
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/usage/by-model");
    expect(result[0].modelName).toBe("deepseek-chat");
  });

  it("getSessionUsageSummary 查询会话用量汇总", async () => {
    const summary = { sessionId: "s1", totalRequests: 3, totalTokens: 100, totalCost: 0.5, byModel: [] };
    httpMock.get.mockResolvedValue({ data: summary });
    const result = await getSessionUsageSummary("s1");
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/s1/usage");
    expect(result.totalRequests).toBe(3);
  });

  it("listSessionUsage 查询会话明细列表", async () => {
    const list = [{ id: 1, sessionId: "s1" }];
    httpMock.get.mockResolvedValue({ data: list });
    const result = await listSessionUsage("s1");
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/s1/usage/list");
    expect(result).toHaveLength(1);
  });
});
