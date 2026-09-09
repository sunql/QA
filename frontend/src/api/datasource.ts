import axios from "axios";
import { httpClient } from "./client";
import { API_BASE_URL, REQUEST_TIMEOUT_MS, DEFAULT_TENANT_ID, DEFAULT_USER_ID } from "../config";
import type {
  DataSource,
  DataSourceCreate,
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
export async function introspectDatasource(
  id: number,
  schema?: string | null,
): Promise<SchemaIntrospectResponse> {
  const res = await httpClient.post<SchemaIntrospectResponse>(`${BASE}/${id}/introspect`, undefined, {
    params: schema ? { schema } : undefined,
  });
  return res.data;
}

// 连接测试返回 { success, message }，其 success 字段与统一信封的 success 冲突：
// 若走 httpClient，success=false 会被拦截器当作失败信封，丢失真实 message。
// 故使用独立原始 axios 实例，不做信封解包，由调用方根据 success 展示结果。
const rawClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: REQUEST_TIMEOUT_MS,
  headers: {
    "Content-Type": "application/json",
    "X-Tenant-Id": DEFAULT_TENANT_ID,
    "X-User-Id": DEFAULT_USER_ID,
  },
});

export async function testDataSource(
  payload: DataSourceTestRequest
): Promise<DataSourceTestResponse> {
  const res = await rawClient.post<DataSourceTestResponse>(`${BASE}/test`, payload);
  return res.data;
}
