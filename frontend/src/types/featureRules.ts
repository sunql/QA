export type Severity = "HIGH" | "MEDIUM" | "LOW" | "INFO";
export type RuleOperator = "lt" | "lte" | "gt" | "gte" | "lt_inverse";

export interface FeatureRuleThreshold {
  severity: Severity;
  operator: RuleOperator;
  threshold_value: number;
  unit?: string | null;
  threshold_order: number;
}

export interface FeatureRule {
  id: number;
  code: string;
  data_object: string;
  data_layer: string;
  target_level: string;
  feature_name: string;
  enabled: boolean;
  priority: number;
  policy_description?: string | null;
  version: number;
  thresholds: FeatureRuleThreshold[];
  created_time: string;
  updated_time?: string | null;
}

export interface FeatureRuleCreate {
  code: string;
  data_object: string;
  data_layer: string;
  target_level: string;
  feature_name: string;
  enabled?: boolean;
  priority?: number;
  policy_description?: string | null;
  thresholds: FeatureRuleThreshold[];
}

export interface FeatureRuleUpdate {
  enabled?: boolean;
  priority?: number;
  policy_description?: string | null;
  thresholds?: FeatureRuleThreshold[];
  version: number;
}

export interface FeatureRuleThresholdSuggestion {
  feature_name: string;
  severity: Severity;
  operator: RuleOperator;
  threshold_value: number;
  unit?: string | null;
  confidence: number;
  rationale: string;
}

export interface FeatureRuleParseDescriptionRequest {
  data_object: string;
  data_layer: string;
  target_level: string;
  natural_language: string;
}

export interface FeatureRuleParseDescriptionResponse {
  suggested_thresholds: FeatureRuleThresholdSuggestion[];
  reasoning: string;
  overall_confidence: number;
  warnings: string[];
}
