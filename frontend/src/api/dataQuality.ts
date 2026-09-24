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
  // array 值转成 axios 期望的 paramsSerializer 形式：?targetTables=A&targetTables=B
  // FastAPI list[str] 既支持重复同名参数也支持逗号分隔；这里走更标准的重复参数。
  const flat: Record<string, string | number | boolean | string[]> = {};
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      flat[k] = v as string | number | boolean | string[];
    }
  }
  const res = await httpClient.get<DataQualityRule[]>(BASE, { params: flat });
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