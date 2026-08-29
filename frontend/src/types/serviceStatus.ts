// 依赖服务健康状态（与后端 ServiceStatus / ServiceStatusResponse 对齐）

export type ServiceHealth = "up" | "down" | "not_configured";

export interface ServiceStatus {
  name: string;
  status: ServiceHealth;
  latencyMs: number | null;
  endpoint: string | null;
  detail: string | null;
}

export interface ServiceStatusResponse {
  services: ServiceStatus[];
  checkedAt: string;
}
