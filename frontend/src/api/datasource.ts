import axios from "axios";
import { httpClient } from "./client";
import { API_BASE_URL, REQUEST_TIMEOUT_MS, DEFAULT_TENANT_ID } from "../config";
import type {
  DataSource,
  DataSourceCreate,
  DataSourceTestRequest,
  DataSourceTestResponse,
  DataSourceUpdate,
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

// 连接测试返回 { success, message }，其 success 字段与统一信封的 success 冲突：
// 若走 httpClient，success=false 会被拦截器当作失败信封，丢失真实 message。
// 故使用独立原始 axios 实例，不做信封解包，由调用方根据 success 展示结果。
// 不再硬编码 X-User-Id：身份由后端 stub 路径或 JWT/IdP 接管（#60 件1）。
const rawClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: REQUEST_TIMEOUT_MS,
  headers: {
    "Content-Type": "application/json",
    "X-Tenant-Id": DEFAULT_TENANT_ID,
  },
});

export async function testDataSource(
  payload: DataSourceTestRequest
): Promise<DataSourceTestResponse> {
  const res = await rawClient.post<DataSourceTestResponse>(`${BASE}/test`, payload);
  return res.data;
}
