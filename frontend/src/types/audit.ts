/** audit_log 类型定义（Phase 4.5 治理 API）。

对齐后端 AuditLogRead（Pydantic CamelModel → 前端 camelCase）。
*/

export interface AuditLog {
  id: number;
  entityType: string;
  entityId: number;
  action: string;
  actor: string;
  actorDepartments: string | null;
  beforeJson: Record<string, unknown> | null;
  afterJson: Record<string, unknown> | null;
  createdAt: string; // ISO datetime string
}

export interface AuditLogFilters {
  entityType?: string;
  entityId?: string;
  actor?: string;
  since?: string; // ISO date string YYYY-MM-DD
  until?: string; // ISO date string YYYY-MM-DD
  limit?: number;
  offset?: number;
}
