/** 数据质量评估与评分类型 — 对应后端 Phase 1.2 / 1.3 schema（camelCase 契约）。 */

import type { RuleType } from "./dataQuality";

// ---------------------------------------------------------------------------
// Phase 1.2 评估执行
// ---------------------------------------------------------------------------

/** 单条规则的评估结果。
 *
 * status 取值：
 * - "PASS" 通过（pass_rate ≥ threshold）
 * - "FAIL" 未通过（pass_rate < threshold；message 含具体原因，feat-eval-fail-reason）
 * - "ERROR" 评估异常（SQL 报错 / 业务库挂 / 数据源缺失等；message 含异常文本）
 */
export type EvaluationStatus = "PASS" | "FAIL" | "ERROR";

export interface EvaluationResult {
  ruleId: number;
  ruleCode: string;
  ruleType: RuleType;
  datasourceId: number | null;
  totalCount: number;
  passedCount: number;
  passRate: number; // 0.0 ~ 100.0
  status: EvaluationStatus;
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

/** 评分维度枚举 — 与后端 ScoreType 枚举（backend/app/domain/enums.py）一致。
 *
 * 注意：后端只有 TABLE / GLOBAL；前端曾误写 "DATABASE" | "COLUMN" 会让下拉筛选
 * 选到非法值直接 422。DATASET 聚合粒度在 ScoreType 注释里标注为「Phase 3+ 决定」，
 * 所以这次没在 enum 里加。 */
export type ScoreType = "TABLE" | "GLOBAL";

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

/** 触发 compute 的请求体（feat-dq-scores-scope，2026-09-15；multiselect）。
 *
 * 三字段全 optional；不传 = 现有全量行为（向后兼容）。
 * targetTables / ruleTypes 支持多选；datasourceId 仍单选。
 * 三条件 AND 组合；scope 命中 0 条规则时后端返回空响应、不写库、不写 GLOBAL。 */
export interface ComputeScoresRequest {
  datasourceId?: number;
  targetTables?: string[];
  ruleTypes?: RuleType[];
}

/** 评分列表查询参数。 */
export interface ScoreListParams {
  table?: string;
  scoreType?: ScoreType;
  latest?: boolean;
  limit?: number;
}
