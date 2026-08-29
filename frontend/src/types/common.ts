// 统一响应信封（与后端 ApiResponse 对齐）
export interface ApiResponse<T> {
  success: boolean;
  data: T | null;
  error: string | null;
  timestamp: string;
}

// 健康检查响应
export interface HealthResponse {
  status: string;
  version: string;
  timestamp: string;
}
