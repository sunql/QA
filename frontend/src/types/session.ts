// 单次 Token 使用记录（对齐后端 SessionTokenUsageRead）
export interface SessionTokenUsage {
  id: number;
  sessionId: string;
  modelName: string | null;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  cost: number;
  requestTime: string;
  purpose: string | null;
}

// 单模型统计（对齐后端 ModelUsageStat）
export interface ModelUsageStat {
  modelName: string;
  requests: number;
  totalTokens: number;
  totalCost: number;
}

// 会话使用量汇总
export interface TokenUsageSummary {
  sessionId: string;
  totalRequests: number;
  totalTokens: number;
  totalCost: number;
  byModel: ModelUsageStat[];
}

// 会话列表项（用量看板）
export interface SessionListItem {
  sessionId: string;
  totalRequests: number;
  totalTokens: number;
  totalCost: number;
  firstRequestTime: string;
  lastRequestTime: string;
  lastQuestion: string | null;
}

// 全局用量摘要（对齐后端 GlobalUsageSummary，用量看板顶部卡片）
export interface GlobalUsageSummary {
  totalSessions: number;
  totalRequests: number;
  totalTokens: number;
  totalCost: number;
}

// 单日全局用量趋势点（对齐后端 DailyUsageTrend）
export interface DailyUsageTrend {
  date: string;
  requests: number;
  tokens: number;
  cost: number;
}

// 按模型汇总的全局用量（对齐后端 ModelUsageAggregate）
export interface ModelUsageAggregate {
  modelName: string;
  requests: number;
  tokens: number;
  cost: number;
}
