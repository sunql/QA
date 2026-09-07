/**
 * FeatureRule 前端类型 — camelCase，与后端 Pydantic CamelModel 对齐。
 *
 * 历史教训：原本全用 snake_case，但后端 JSON 是 camelCase（alias_generator=to_camel），
 * 导致表格 dataIndex 找不到字段、编辑表单 setFieldsValue 失败。
 * 其他 admin 模块（AgentRegistry、AdminTools）都用 camelCase，本模块是 outlier，已统一。
 */
export type Severity = "HIGH" | "MEDIUM" | "LOW" | "INFO";
export type RuleOperator = "lt" | "lte" | "gt" | "gte" | "lt_inverse";

export interface FeatureRuleThreshold {
  severity: Severity;
  operator: RuleOperator;
  thresholdValue: number | string;
  unit?: string | null;
  thresholdOrder: number;
}

export interface FeatureRule {
  id: number;
  code: string;
  dataObject: string;
  dataLayer: string;
  targetLevel: string;
  featureName: string;
  enabled: boolean;
  priority: number;
  policyDescription?: string | null;
  version: number;
  thresholds: FeatureRuleThreshold[];
  createdTime: string;
  updatedTime?: string | null;
}

export interface FeatureRuleCreate {
  code: string;
  dataObject: string;
  dataLayer: string;
  targetLevel: string;
  featureName: string;
  enabled?: boolean;
  priority?: number;
  policyDescription?: string | null;
  thresholds: FeatureRuleThreshold[];
}

export interface FeatureRuleUpdate {
  enabled?: boolean;
  priority?: number;
  policyDescription?: string | null;
  thresholds?: FeatureRuleThreshold[];
  version: number;
}

export interface FeatureRuleThresholdSuggestion {
  featureName: string;
  severity: Severity;
  operator: RuleOperator;
  thresholdValue: number;
  unit?: string | null;
  confidence: number;
  rationale: string;
}

export interface FeatureRuleParseDescriptionRequest {
  dataObject: string;
  dataLayer: string;
  targetLevel: string;
  naturalLanguage: string;
}

export interface FeatureRuleParseDescriptionResponse {
  suggestedThresholds: FeatureRuleThresholdSuggestion[];
  reasoning: string;
  overallConfidence: number;
  warnings: string[];
}