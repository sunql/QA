/**
 * 数据质量规则自动生成 — 前端类型定义。
 * 对应后端 Task 4/5/6 契约（camelCase JSON）。
 */

import type { DataQualityRule } from "./dataQuality";
import type { RuleType } from "./dataQuality";
import type { Severity } from "./dataQuality";

// ---------------------------------------------------------------------------
// 枚举/联合类型（与 dataQuality.ts 保持一致）
// ---------------------------------------------------------------------------

/** 阈值推导方式（与后端 DerivationType 枚举对齐）。 */
export type DerivationType =
  | "PK_DERIVED"
  | "FK_DERIVED"
  | "DICT_REF"
  | "ALLOWED_VALUES"
  | "NOT_NULL"
  | "JOIN_CONSISTENCY"
  | "LLM_DERIVED"
  | "MANUAL";

export type RuleSuggestionStatus = "NEW" | "EXISTS";

export type ConstraintKind = "allowed_values" | "not_null";

// ---------------------------------------------------------------------------
// RuleSuggestion — GeneratorService.preview 返回的单条建议
// ---------------------------------------------------------------------------

export interface RuleSuggestion {
  ruleCode: string;
  ruleName: string;
  ruleType: RuleType;
  targetTable: string;
  targetColumn: string | null;
  ruleExpression: string | null;
  threshold: number;
  severity: Severity;
  derivationType: DerivationType;
  sourcePropertyId: number | null;
  sourceClassId: number | null;
  confidence: string;
  status: RuleSuggestionStatus;
  reason: string;
}

// ---------------------------------------------------------------------------
// GeneratePreview — previewRules 返回
// ---------------------------------------------------------------------------

export interface BlockedProperty {
  propertyName: string;
  reason: string;
}

export interface GeneratePreviewResponse {
  classId: number;
  className: string;
  sourceTable: string | null;
  datasourceId: number;
  suggestions: RuleSuggestion[];
  blocked: BlockedProperty[];
}

// ---------------------------------------------------------------------------
// GenerateConfirm — confirmRules 返回
// ---------------------------------------------------------------------------

export interface GenerateConfirmResponse {
  created: DataQualityRule[];
  skippedCodes: string[];
}

// ---------------------------------------------------------------------------
// PropertyConstraintSuggestion — parseDescriptions / applySuggestion 用
// ---------------------------------------------------------------------------

export interface PropertyConstraintSuggestion {
  propertyId: number;
  propertyName: string;
  kind: ConstraintKind;
  values: string[] | null;
  confidence: number;
  rationale: string;
}

// ---------------------------------------------------------------------------
// ParseDescriptionsResponse — parseDescriptions 完整 envelope（含 persistedPropertyIds）
// ---------------------------------------------------------------------------

export interface ParseDescriptionsResponse {
  suggestions: PropertyConstraintSuggestion[];
  /** 当前类下已在 ontology_property 写入 allowed_values 的 propertyId 列表；
   *  LlmPanel 用它初始化 adoptedIds，实现刷新页面也保持已采纳状态。 */
  persistedPropertyIds: number[];
}

// ---------------------------------------------------------------------------
// LlmModelOption — listLlmModels 返回的最小子集
// （避免在前端 import 完整 ModelConfig，这里只取向导需要的字段）
// ---------------------------------------------------------------------------

export type LlmProvider = "OPENAI" | "AZURE_OPENAI" | "OPENAI_COMPATIBLE_PROXY" | "OLLAMA";

export interface LlmModelOption {
  id: number;
  modelName: string;
  provider: LlmProvider;
}
