/** audit_log API client（Phase 4.5 治理 API）。 */
import { httpClient } from "./client";
import type { AuditLog, AuditLogFilters } from "../types/audit";

const BASE = "/audit";

/**
 * GET /api/v1/audit
 * 全局查询审计日志，支持 entity_type / actor / limit / offset 过滤。
 */
export async function listAuditLogs(filters: AuditLogFilters = {}): Promise<AuditLog[]> {
  const res = await httpClient.get<AuditLog[]>(BASE, { params: filters });
  return res.data;
}

/**
 * GET /api/v1/audit/{id}
 * 按 ID 查询单条审计记录。
 */
export async function getAuditLog(id: number): Promise<AuditLog> {
  const res = await httpClient.get<AuditLog>(`${BASE}/${id}`);
  return res.data;
}
