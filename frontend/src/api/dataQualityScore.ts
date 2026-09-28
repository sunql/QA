import { httpClient } from "./client";
import type {
  EvaluationResult,
  EvaluateBatchRequest,
  EvaluateBatchResponse,
  DataQualityScore,
  ComputeScoresRequest,
  ComputeScoresResponse,
  ScoreListParams,
} from "../types/dataQualityScore";

const BASE = "/data-quality";

// ---------------------------------------------------------------------------
// Phase 1.2 评估执行
// ---------------------------------------------------------------------------

/** POST /api/v1/data-quality/rules/{ruleId}/evaluate — 触发单条规则评估。 */
export async function evaluateRule(ruleId: number): Promise<EvaluationResult> {
  const res = await httpClient.post<EvaluationResult>(
    `${BASE}/rules/${ruleId}/evaluate`,
  );
  return res.data;
}

/** POST /api/v1/data-quality/rules/evaluate-batch — 批量评估。 */
export async function evaluateBatch(
  payload: EvaluateBatchRequest,
): Promise<EvaluateBatchResponse> {
  const res = await httpClient.post<EvaluateBatchResponse>(
    `${BASE}/rules/evaluate-batch`,
    payload,
  );
  return res.data;
}

// ---------------------------------------------------------------------------
// Phase 1.3 评分
// ---------------------------------------------------------------------------

/** POST /api/v1/data-quality/scores/compute — 触发全量评估 + 聚合落库。
 *
 * 可选 payload：datasourceId / targetTable / ruleType 三字段全 optional；不传 = 全量。
 * 三条件 AND 组合；scope 命中 0 条规则时后端返回空响应、不写库、不写 GLOBAL。 */
export async function computeScore(
  payload?: ComputeScoresRequest,
): Promise<ComputeScoresResponse> {
  const res = await httpClient.post<ComputeScoresResponse>(
    `${BASE}/scores/compute`,
    payload ?? {},
  );
  return res.data;
}

/** GET /api/v1/data-quality/scores — 查询评分列表。 */
export async function listScores(
  filters?: ScoreListParams,
): Promise<DataQualityScore[]> {
  const res = await httpClient.get<DataQualityScore[]>(`${BASE}/scores`, {
    params: filters as Record<string, string | boolean | number | undefined>,
  });
  return res.data;
}
