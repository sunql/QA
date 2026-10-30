// 全局配置：API 基地址与默认请求头
const rawBase = import.meta.env.VITE_API_BASE_URL as string | undefined;
export const API_BASE_URL = rawBase && rawBase.length > 0 ? rawBase : "/api/v1";

// 请求超时（毫秒）
export const REQUEST_TIMEOUT_MS = 30_000;

// 长操作 per-call 超时（毫秒）：批量关系引擎 / 图谱对账这类重操作在生产
// 数据量下超 30s ⇒ axios 掐断 ⇒ 前端误报「网络异常」。上限对齐
// docker/nginx.conf 的 proxy_read_timeout=300s 并留余量取 280s
// （2026-10-23 CentOS7 首装实测；SSOT 见 ontologyBatchApiTimeout.test.ts）。
export const LONG_RUNNING_TIMEOUT_MS = 280_000;

// 默认租户（Phase 1 单租户占位；feat-user-auth 之后 X-User-Id 不再硬编码，
// 改由 authStore.token 走 Authorization: Bearer 注入）
export const DEFAULT_TENANT_ID = "default";
