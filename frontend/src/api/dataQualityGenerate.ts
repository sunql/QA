/**
 * 数据质量规则自动生成 — API client。
 * 对应后端 Task 4/5/6 端点。
 */

import { httpClient } from "./client";
import type {
  GeneratePreviewResponse,
  GenerateConfirmResponse,
  PropertyConstraintSuggestion,
  RuleSuggestion,
} from "../types/dataQualityGenerate";

const BASE = "/data-quality/rules/generate";

// ---------------------------------------------------------------------------
// Task 4: 预览规则建议
// ---------------------------------------------------------------------------

/**
 * 预览：给定本体类 + 数据源，调用 GeneratorService.preview，
 * 返回候选规则建议列表及被屏蔽的属性。
 */
export async function previewRules(
  classId: number,
  datasourceId: number,
): Promise<GeneratePreviewResponse> {
  const res = await httpClient.post<GeneratePreviewResponse>(`${BASE}/preview`, {
    classId,
    datasourceId,
  });
  return res.data;
}

// ---------------------------------------------------------------------------
// Task 5: 确认并创建规则
// ---------------------------------------------------------------------------

/**
 * 确认：批量写入用户采纳的规则建议，返回实际创建成功的规则列表
 * 及因重复而跳过的 ruleCode 列表。
 */
export async function confirmRules(
  datasourceId: number,
  rules: RuleSuggestion[],
): Promise<GenerateConfirmResponse> {
  const res = await httpClient.post<GenerateConfirmResponse>(`${BASE}/confirm`, {
    datasourceId,
    rules,
  });
  return res.data;
}

// ---------------------------------------------------------------------------
// Task 6: LLM 从属性描述中提取候选约束
// ---------------------------------------------------------------------------

/**
 * 解析本体类的属性描述，LLM 推断候选约束（allowed_values / not_null）。
 */
export async function parseDescriptions(
  classId: number,
): Promise<PropertyConstraintSuggestion[]> {
  const res = await httpClient.post<{ suggestions: PropertyConstraintSuggestion[] }>(
    `${BASE}/parse-descriptions`,
    { classId },
  );
  return res.data.suggestions;
}

/**
 * 采纳 LLM 推荐的 allowed_values，写入 ontology_property 表。
 */
export async function applySuggestion(
  propertyId: number,
  allowedValues: string[],
): Promise<void> {
  await httpClient.post(`${BASE}/apply-suggestion`, {
    propertyId,
    allowedValues,
  });
}
