/**
 * 数据质量规则自动生成 — API client。
 * 对应后端 Task 4/5/6 端点。
 */

import { httpClient } from "./client";
import type {
  GeneratePreviewResponse,
  GenerateConfirmResponse,
  LlmModelOption,
  ParseDescriptionsResponse,
  RuleSuggestion,
} from "../types/dataQualityGenerate";

const BASE = "/data-quality/rules/generate";
const MODELS_BASE = "/models";

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
 * 列出当前可用的 LLM 模型配置（供向导下拉选择）。
 * 调用 GET /api/v1/models?activeOnly=true。
 *
 * 关键：query 参数名是 camelCase（与 FastAPI kwarg 名一致），
 * 写 snake_case 会被静默忽略，永远拿不到 activeOnly 过滤后的列表。
 */
export async function listLlmModels(): Promise<LlmModelOption[]> {
  const res = await httpClient.get<LlmModelOption[]>(MODELS_BASE, {
    params: { activeOnly: true },
  });
  return res.data.map((m) => ({
    id: m.id,
    modelName: m.modelName,
    provider: m.provider,
  }));
}

/**
 * 解析本体类的属性描述，LLM 推断候选约束（allowed_values / not_null）。
 * 返回完整 envelope（含 persistedPropertyIds），前端用它初始化 LlmPanel.adoptedIds。
 * `modelId` 可选：传入则路由层走对应 ModelConfig 创建 client，否则走默认 env 路径。
 */
export async function parseDescriptions(
  classId: number,
  modelId?: number | null,
): Promise<ParseDescriptionsResponse> {
  const res = await httpClient.post<ParseDescriptionsResponse>(
    `${BASE}/parse-descriptions`,
    { classId, modelId: modelId ?? null },
  );
  return res.data;
}

/**
 * 采纳 LLM 推荐的约束，按 kind 派发写入 ontology_property 表（feat-ontology-property-constraints）。
 * - allowed_values：传 allowedValues
 * - not_null      ：无需额外字段
 * - range         ：传 minValue + maxValue
 * - pattern       ：传 regexPattern
 *
 * 后端 schema 默认 kind=allowed_values 以兼容旧 client。
 */
export type SuggestionKind =
  | "allowed_values"
  | "not_null"
  | "range"
  | "pattern";

export interface ApplySuggestionPayload {
  kind: SuggestionKind;
  allowedValues?: string[];
  minValue?: string;
  maxValue?: string;
  regexPattern?: string;
}

export async function applySuggestion(
  propertyId: number,
  payload: ApplySuggestionPayload | string[],
): Promise<void> {
  // 兼容旧调用：传 string[] 时当作 allowed_values。
  const body: Record<string, unknown> = { propertyId };
  if (Array.isArray(payload)) {
    body.kind = "allowed_values";
    body.allowedValues = payload;
  } else {
    body.kind = payload.kind;
    if (payload.kind === "allowed_values") body.allowedValues = payload.allowedValues ?? [];
    if (payload.kind === "range") {
      body.minValue = payload.minValue;
      body.maxValue = payload.maxValue;
    }
    if (payload.kind === "pattern") body.regexPattern = payload.regexPattern;
  }
  await httpClient.post(`${BASE}/apply-suggestion`, body);
}
