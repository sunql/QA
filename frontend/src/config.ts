// 全局配置：API 基地址与默认请求头
const rawBase = import.meta.env.VITE_API_BASE_URL as string | undefined;
export const API_BASE_URL = rawBase && rawBase.length > 0 ? rawBase : "/api/v1";

// 请求超时（毫秒）
export const REQUEST_TIMEOUT_MS = 30_000;

// 默认租户与用户头（Phase 1 单租户占位）
export const DEFAULT_TENANT_ID = "default";
export const DEFAULT_USER_ID = "system";
