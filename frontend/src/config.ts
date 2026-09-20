// 全局配置：API 基地址与默认请求头
const rawBase = import.meta.env.VITE_API_BASE_URL as string | undefined;
export const API_BASE_URL = rawBase && rawBase.length > 0 ? rawBase : "/api/v1";

// 请求超时（毫秒）
export const REQUEST_TIMEOUT_MS = 30_000;

// 默认租户（Phase 1 单租户占位；feat-user-auth 之后 X-User-Id 不再硬编码，
// 改由 authStore.token 走 Authorization: Bearer 注入）
export const DEFAULT_TENANT_ID = "default";
