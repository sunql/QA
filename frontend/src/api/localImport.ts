import axios from "axios";
import { httpClient } from "./client";
import {
  API_BASE_URL,
  REQUEST_TIMEOUT_MS,
  DEFAULT_TENANT_ID,
  DEFAULT_USER_ID,
} from "../config";
import type {
  ImportExecuteRequest,
  ImportExecuteResponse,
  ImportPreviewResponse,
  ImportPreviewRequest,
} from "../types/localImport";

export async function getImportPreview(
  datasourceId: number,
  request: ImportPreviewRequest
): Promise<ImportPreviewResponse> {
  const res = await httpClient.post<ImportPreviewResponse>(
    `/datasources/${datasourceId}/import-preview`,
    request
  );
  return res.data;
}

// 执行导入返回 ImportExecuteResponse，其顶层 success 字段与统一信封的 success 冲突：
// 若走 httpClient，拦截器会把 success=false 当作失败信封并 reject，丢失真实 errors 数组。
// 故使用独立原始 axios 实例（对齐 datasource.ts 的 testDataSource），不做信封解包，
// 由调用方根据 success / errors 展示结果。
const rawClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: REQUEST_TIMEOUT_MS,
  headers: {
    "Content-Type": "application/json",
    "X-Tenant-Id": DEFAULT_TENANT_ID,
    "X-User-Id": DEFAULT_USER_ID,
  },
});

export async function executeImport(
  datasourceId: number,
  request: ImportExecuteRequest
): Promise<ImportExecuteResponse> {
  const res = await rawClient.post<ImportExecuteResponse>(
    `/datasources/${datasourceId}/import`,
    request
  );
  return res.data;
}
