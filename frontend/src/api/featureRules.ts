import { httpClient } from "./client";
import type {
  FeatureRule,
  FeatureRuleCreate,
  FeatureRuleUpdate,
  FeatureRuleParseDescriptionRequest,
  FeatureRuleParseDescriptionResponse,
} from "../types/featureRules";

const PREFIX = "/feature-rules";

export async function listFeatureRules(params?: {
  enabledOnly?: boolean;
}): Promise<FeatureRule[]> {
  const res = await httpClient.get<FeatureRule[]>(PREFIX, {
    params: params?.enabledOnly ? { enabledOnly: true } : {},
  });
  return res.data;
}

export async function getFeatureRule(code: string): Promise<FeatureRule> {
  const res = await httpClient.get<FeatureRule>(
    `${PREFIX}/${encodeURIComponent(code)}`,
  );
  return res.data;
}

export async function createFeatureRule(
  payload: FeatureRuleCreate,
): Promise<FeatureRule> {
  const res = await httpClient.post<FeatureRule>(PREFIX, payload);
  return res.data;
}

export async function updateFeatureRule(
  code: string,
  payload: FeatureRuleUpdate,
): Promise<FeatureRule> {
  const res = await httpClient.put<FeatureRule>(
    `${PREFIX}/${encodeURIComponent(code)}`,
    payload,
  );
  return res.data;
}

export async function deleteFeatureRule(code: string): Promise<void> {
  await httpClient.delete(`${PREFIX}/${encodeURIComponent(code)}`);
}

export async function toggleFeatureRule(
  code: string,
  enabled: boolean,
): Promise<FeatureRule> {
  const res = await httpClient.post<FeatureRule>(
    `${PREFIX}/${encodeURIComponent(code)}/toggle`,
    { enabled },
  );
  return res.data;
}

export async function parseFeatureRuleDescription(
  payload: FeatureRuleParseDescriptionRequest,
): Promise<FeatureRuleParseDescriptionResponse> {
  const res = await httpClient.post<FeatureRuleParseDescriptionResponse>(
    `${PREFIX}/parse-description`,
    payload,
  );
  return res.data;
}
