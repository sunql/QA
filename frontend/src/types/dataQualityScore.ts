/** 数据质量评估与评分类型 — 对应后端 Phase 1.2 / 1.3 schema（camelCase 契约）。 */

import type { RuleType } from "./dataQuality";

// ---------------------------------------------------------------------------
// Phase 1.2 评估执行
// ---------------------------------------------------------------------------

/** 单条规则的评估结果。status=PASS 时表示 pass_rate >= rule.threshold。 */
export interface EvaluationResult {
  ruleId: number;
  ruleCode: string;
  ruleType: RuleType;
  datasourceId: number | null;
  totalCount: number;
  passedCount: number;
  passRate: number; // 0.0 ~ 100.0
  status: "PASS" | "FAIL";
  evaluatedAt: string; // ISO datetime
  durationMs: number;
  message: string | null;
}

/** 批量评估请求体。 */
export interface EvaluateBatchRequest {
  ruleIds: number[];
}

/** 批量评估响应。 */
export interface EvaluateBatchResponse {
  results: EvaluationResult[];
  summaryTotal: number;
  summaryPassed: number;
}

// ---------------------------------------------------------------------------
// Phase 1.3 评分
// ---------------------------------------------------------------------------

/** 评分维度枚举 — 与后端 ScoreType 枚举一致。 */
export type ScoreType = "TABLE" | "DATABASE" | "COLUMN";

/** 数据质量评分历史记录。 */
export interface DataQualityScore {
  id: number;
  targetTable: string;
  scoreType: ScoreType;
  completenessScore: string | null; // Decimal → string
  validityScore: string | null;
  uniquenessScore: string | null;
  consistencyScore: string | null;
  timelinessScore: string | null;
  referentialScore: string | null;
  overallScore: string; // Decimal
  evaluatedAt: string; // ISO datetime
  evaluationDurationMs: number;
  rulesCount: number;
  createdTime: string | null;
  updatedTime: string | null;
}

/** 触发 compute 后的响应。 */
export interface ComputeScoresResponse {
  evaluatedRules: number;
  savedScores: number;
  durationMs: number;
  scores: DataQualityScore[];
}

/** 评分列表查询参数。 */
export interface ScoreListParams {
  table?: string;
  scoreType?: ScoreType;
  latest?: boolean;
  limit?: number;
}
