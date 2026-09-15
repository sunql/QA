/** 数据质量规则配置 API client（feat-dq-rule-params，2026-09-15） */

import { httpClient } from "./client";
import type {
  RuleParamsReadDto,
  RuleParamsCreateDto,
  RuleParamsUpdateDto,
} from "../types/dataQualityRuleParams";

const BASE = "/dq-rule-params/rules";

export async function listRules(datasourceId?: number): Promise<RuleParamsReadDto[]> {
  const res = await httpClient.get<RuleParamsReadDto[]>(BASE, {
    params: datasourceId !== undefined ? { datasourceId } : undefined,
  });
  return res.data;
}

export async function getRule(id: number): Promise<RuleParamsReadDto> {
  const res = await httpClient.get<RuleParamsReadDto>(`${BASE}/${id}`);
  return res.data;
}

export async function createRule(payload: RuleParamsCreateDto): Promise<RuleParamsReadDto> {
  const res = await httpClient.post<RuleParamsReadDto>(BASE, payload);
  return res.data;
}

export async function updateRule(
  id: number,
  payload: RuleParamsUpdateDto,
): Promise<RuleParamsReadDto> {
  const res = await httpClient.put<RuleParamsReadDto>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function deleteRule(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}
