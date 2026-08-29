import { httpClient } from "./client";
import type {
  DailyUsageTrend,
  GlobalUsageSummary,
  ModelUsageAggregate,
  SessionListItem,
  SessionTokenUsage,
  TokenUsageSummary,
} from "../types/session";

export async function listSessions(): Promise<SessionListItem[]> {
  const res = await httpClient.get<SessionListItem[]>("/sessions");
  return res.data;
}

// 全局用量聚合（用量看板顶部卡片 + 趋势图）
export async function getGlobalUsageSummary(): Promise<GlobalUsageSummary> {
  const res = await httpClient.get<GlobalUsageSummary>("/sessions/usage/global");
  return res.data;
}

export async function getDailyUsageTrends(): Promise<DailyUsageTrend[]> {
  const res = await httpClient.get<DailyUsageTrend[]>("/sessions/usage/daily");
  return res.data;
}

export async function getModelUsage(): Promise<ModelUsageAggregate[]> {
  const res = await httpClient.get<ModelUsageAggregate[]>("/sessions/usage/by-model");
  return res.data;
}

export async function getSessionUsageSummary(
  sessionId: string
): Promise<TokenUsageSummary> {
  const res = await httpClient.get<TokenUsageSummary>(
    `/sessions/${sessionId}/usage`
  );
  return res.data;
}

export async function listSessionUsage(
  sessionId: string
): Promise<SessionTokenUsage[]> {
  const res = await httpClient.get<SessionTokenUsage[]>(
    `/sessions/${sessionId}/usage/list`
  );
  return res.data;
}
