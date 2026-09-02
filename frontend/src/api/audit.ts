/** audit_log API client（Phase 4.5 治理 API）。 */
import { httpClient } from "./client";
import type { AuditLog, AuditLogFilters, AuditLogPage } from "../types/audit";

const BASE = "/audit";

/**
 * GET /api/v1/audit
 * 全局查询审计日志，支持 entity_type / actor / limit / offset 过滤。
 */
export async function listAuditLogs(filters: AuditLogFilters = {}): Promise<AuditLogPage> {
  const res = await httpClient.get<AuditLogPage>(BASE, { params: filters });
  return res.data;
}

/**
 * GET /api/v1/audit/export?format=csv|json
 * 导出审计日志为 CSV 或 JSON Lines 格式。
 */
export async function exportAuditLogs(
  filters: AuditLogFilters,
  format: "csv" | "json",
): Promise<Blob> {
  const res = await httpClient.get(`${BASE}/export`, {
    params: { ...filters, format },
    responseType: "blob",
  });
  return res.data as unknown as Blob;
}

/**
 * GET /api/v1/audit/{id}
 * 按 ID 查询单条审计记录。
 */
export async function getAuditLog(id: number): Promise<AuditLog> {
  const res = await httpClient.get<AuditLog>(`${BASE}/${id}`);
  return res.data;
}
