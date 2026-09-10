import { httpClient } from "./client";
import type {
  DataQualityRule,
  DataQualityRuleCreate,
  DataQualityRuleListParams,
  DataQualityRuleUpdate,
  RuleOptions,
} from "../types/dataQuality";

const BASE = "/data-quality/rules";

export async function listRules(
  params?: DataQualityRuleListParams,
): Promise<DataQualityRule[]> {
  const res = await httpClient.get<DataQualityRule[]>(BASE, {
    params: params as Record<string, string | boolean | number | undefined>,
  });
  return res.data;
}

export async function listRuleOptions(): Promise<RuleOptions> {
  const res = await httpClient.get<RuleOptions>(`${BASE}/options`);
  return res.data;
}

export async function getRule(id: number): Promise<DataQualityRule> {
  const res = await httpClient.get<DataQualityRule>(`${BASE}/${id}`);
  return res.data;
}

export async function createRule(
  payload: DataQualityRuleCreate,
): Promise<DataQualityRule> {
  const res = await httpClient.post<DataQualityRule>(BASE, payload);
  return res.data;
}

export async function updateRule(
  id: number,
  payload: DataQualityRuleUpdate,
): Promise<DataQualityRule> {
  const res = await httpClient.put<DataQualityRule>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function disableRule(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}