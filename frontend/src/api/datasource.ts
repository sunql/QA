import axios from "axios";
import { httpClient } from "./client";
import { API_BASE_URL, LONG_RUNNING_TIMEOUT_MS, REQUEST_TIMEOUT_MS } from "../config";
import { useAuthStore } from "../stores/authStore";
import type {
  DataSource,
  DataSourceCreate,
  DataSourceLayersRead,
  DataSourceTestRequest,
  DataSourceTestResponse,
  DataSourceUpdate,
  SchemaIntrospectResponse,
} from "../types/datasource";

const BASE = "/datasources";

export async function listDataSources(activeOnly = false): Promise<DataSource[]> {
  const res = await httpClient.get<DataSource[]>(BASE, {
    params: { activeOnly },
  });
  return res.data;
}

export async function getDataSource(id: number): Promise<DataSource> {
  const res = await httpClient.get<DataSource>(`${BASE}/${id}`);
  return res.data;
}

export async function createDataSource(payload: DataSourceCreate): Promise<DataSource> {
  const res = await httpClient.post<DataSource>(BASE, payload);
  return res.data;
}

export async function updateDataSource(
  id: number,
  payload: DataSourceUpdate
): Promise<DataSource> {
  const res = await httpClient.put<DataSource>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function deleteDataSource(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}

// 列出数据源可选 schema（Oracle owner 命名空间）；PG/MySQL 返回空数组（单 schema，无需选择）。
export async function listDatasourceSchemas(id: number): Promise<string[]> {
  const res = await httpClient.get<string[]>(`${BASE}/${id}/schemas`);
  return res.data;
}

// 读取数据源已缓存的 schema；未缓存时后端返回 404（调用方据此触发 introspect）。
// schema 缺省 = 连接用户默认 owner。
export async function getDatasourceSchema(
  id: number,
  schema?: string | null,
): Promise<SchemaIntrospectResponse> {
  const res = await httpClient.get<SchemaIntrospectResponse>(`${BASE}/${id}/schema`, {
    params: schema ? { schema } : undefined,
  });
  return res.data;
}

// 触发数据源 schema 发现并写缓存（数据未变化时复用缓存），返回全量 schema。
// schema 缺省 = 连接用户默认 owner。
//
// 超时必须用 LONG_RUNNING_TIMEOUT_MS：全库内省是重操作（Oracle 大 owner 可达数分钟），
// 走默认 30s 会被 axios 掐断 ⇒ status=0 ⇒ 前端误报「网络异常，请稍后重试」
// （2026-11-25 本地实测 LC019999）。与 ontology 批量重操作同口径。
export async function introspectDatasource(
  id: number,
  schema?: string | null,
): Promise<SchemaIntrospectResponse> {
  const res = await httpClient.post<SchemaIntrospectResponse>(`${BASE}/${id}/introspect`, undefined, {
    params: schema ? { schema } : undefined,
    timeout: LONG_RUNNING_TIMEOUT_MS,
  });
  return res.data;
}

export interface IntrospectJob {
  jobId: string;
  status: "running" | "succeeded" | "failed";
  total: number;
  completed: number;
  error?: string | null;
}

// 启动异步 schema 内省任务。同一数据源+schema 已在跑时后端返回既有 jobId
// （并发保护），调用方无需自己去重。
export async function startIntrospectJob(
  id: number,
  schema?: string | null,
): Promise<IntrospectJob> {
  const res = await httpClient.post<IntrospectJob>(
    `${BASE}/${id}/introspect-jobs`,
    undefined,
    {
      params: schema ? { schema } : undefined,
      timeout: REQUEST_TIMEOUT_MS,
    },
  );
  return res.data;
}

// 查询内省任务进度。后端只读内存态，正常毫秒级返回 ⇒ 用默认短超时；
// 「正常进行中不判超时」由调用方的轮询逻辑保证（连续失败才判中断）。
//
// silentErrors：轮询失联时拦截器不再逐次弹「网络异常」toast —— 由调用方在
// 连续失败达到阈值时只提示一次「连接中断」，避免 5 次失败弹 5 次 + 页面提示重复。
export async function getIntrospectJob(
  id: number,
  jobId: string,
): Promise<IntrospectJob> {
  const res = await httpClient.get<IntrospectJob>(
    `${BASE}/${id}/introspect-jobs/${jobId}`,
    { timeout: REQUEST_TIMEOUT_MS, silentErrors: true },
  );
  return res.data;
}

// 连接测试返回 { success, message }，其 success 字段与统一信封的 success 冲突：
// 若走 httpClient，success=false 会被拦截器当作失败信封，丢失真实 message。
// 故使用独立原始 axios 实例，不做信封解包，由调用方根据 success 展示结果。
// 请求拦截器里注入 Authorization（统一模式），但 success 信封解包不在此。
const rawClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: REQUEST_TIMEOUT_MS,
  headers: {
    "Content-Type": "application/json",
  },
});

rawClient.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) {
    config.headers.set("Authorization", `Bearer ${token}`);
  }
  return config;
});

export async function testDataSource(
  payload: DataSourceTestRequest
): Promise<DataSourceTestResponse> {
  const res = await rawClient.post<DataSourceTestResponse>(`${BASE}/test`, payload);
  return res.data;
}

// 数据层分布（feat-data-domain-layer 方案 2）：cache-only，未探测时 fromCache=false。
export async function getDatasourceLayers(
  id: number,
  schema?: string | null,
): Promise<DataSourceLayersRead> {
  const res = await httpClient.get<DataSourceLayersRead>(
    `${BASE}/${id}/layers`,
    { params: schema ? { owner: schema } : undefined },
  );
  return res.data;
}
