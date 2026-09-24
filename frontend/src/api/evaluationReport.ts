/** 数据质量评估报告 — API client（feat-dq-evaluation-report，Phase 5）。
 *
 * 与 `frontend/src/api/dataQualityGenerate.ts` 同风格：使用 `httpClient`（已注入
 * antd message 拦截器）+ camelCase 契约。
 *
 * 端点前缀：`/api/v1/data-quality/reports`
 */

import { httpClient } from "./client";
import type {
  EvaluationReport,
  EvaluationReportCompareRead,
  EvaluationReportCreate,
  EvaluationReportListRead,
  EvaluationReportProgress,
  EvaluationReportShareCreate,
  EvaluationReportShareRead,
  EvaluationReportUpdate,
  ListReportsQuery,
  ViolationSampleRead,
} from "../types/evaluationReport";

const BASE = "/data-quality/reports";

function toQueryParams(q: ListReportsQuery | undefined): Record<string, unknown> {
  if (!q) return {};
  const out: Record<string, unknown> = {};
  if (q.name) out.name = q.name;
  if (q.classId !== undefined) out.classId = q.classId;
  if (q.ruleId !== undefined) out.ruleId = q.ruleId;
  if (q.createdBy) out.createdBy = q.createdBy;
  if (q.start) out.start = q.start;
  if (q.end) out.end = q.end;
  if (q.limit !== undefined) out.limit = q.limit;
  if (q.offset !== undefined) out.offset = q.offset;
  return out;
}

/** 列表（按 name/classId/ruleId/createdBy/date 过滤，分页）。 */
export async function listReports(
  query: ListReportsQuery = {},
): Promise<EvaluationReportListRead> {
  const res = await httpClient.get<EvaluationReportListRead>(BASE, {
    params: toQueryParams(query),
  });
  return res.data;
}

/** 详情（含完整 snapshot）。 */
export async function getReport(id: number): Promise<EvaluationReport> {
  const res = await httpClient.get<EvaluationReport>(`${BASE}/${id}`);
  return res.data;
}

/** 创建（同步计算 snapshot，耗时较长由后端异步处理或阻塞）。 */
export async function createReport(
  payload: EvaluationReportCreate,
): Promise<EvaluationReport> {
  const res = await httpClient.post<EvaluationReport>(BASE, payload);
  return res.data;
}

/** 更新元数据（仅 name/description/tags/status）。 */
export async function updateReport(
  id: number,
  payload: EvaluationReportUpdate,
): Promise<EvaluationReport> {
  const res = await httpClient.patch<EvaluationReport>(
    `${BASE}/${id}`,
    payload,
  );
  return res.data;
}

/** 软删（设 deleted_at）。 */
export async function deleteReport(id: number): Promise<EvaluationReport> {
  const res = await httpClient.delete<EvaluationReport>(`${BASE}/${id}`);
  return res.data;
}

/** 重算 snapshot。 */
export async function regenerateReport(id: number): Promise<EvaluationReport> {
  const res = await httpClient.post<EvaluationReport>(
    `${BASE}/${id}/regenerate`,
  );
  return res.data;
}

/** 违规样本明细（按规则过滤）。 */
export async function listSamples(
  id: number,
  params: { ruleId?: number; limit?: number } = {},
): Promise<ViolationSampleRead[]> {
  const res = await httpClient.get<ViolationSampleRead[]>(
    `${BASE}/${id}/samples`,
    { params },
  );
  return res.data;
}

// ----- Phase 8a: Compare -----

/** 对比两份报告。 */
export async function compareReports(
  leftId: number,
  rightId: number,
): Promise<EvaluationReportCompareRead> {
  const res = await httpClient.get<EvaluationReportCompareRead>(
    `${BASE}/${leftId}/compare`,
    { params: { otherId: rightId } },
  );
  return res.data;
}

// ----- Phase 8b: Share -----

/** 创建分享 token。 */
export async function createShare(
  reportId: number,
  payload: EvaluationReportShareCreate,
): Promise<EvaluationReportShareRead> {
  const res = await httpClient.post<EvaluationReportShareRead>(
    `${BASE}/${reportId}/share`,
    payload,
  );
  return res.data;
}

/** 列出某报告的现存 share。 */
export async function listShares(
  reportId: number,
): Promise<EvaluationReportShareRead[]> {
  const res = await httpClient.get<EvaluationReportShareRead[]>(
    `${BASE}/${reportId}/share`,
  );
  return res.data;
}

/** 撤销 share。 */
export async function revokeShare(shareId: number): Promise<void> {
  await httpClient.delete(`/data-quality/reports/shares/${shareId}`);
}

/** 公开访问（按 token，无登录）。 */
export async function getPublicReport(
  token: string,
): Promise<EvaluationReport> {
  const res = await httpClient.get<EvaluationReport>(
    `${BASE}/share/${token}`,
  );
  return res.data;
}

// ----- Phase 9: Progress (feat-dq-evaluation-report-progress, 2026-09-15) -----

/** 取报告当前评估进度（前端 1s 轮询直到 stage=COMPLETED 或 FAILED）。 */
export async function getReportProgress(
  id: number,
): Promise<EvaluationReportProgress> {
  const res = await httpClient.get<EvaluationReportProgress>(
    `${BASE}/${id}/progress`,
  );
  return res.data;
}
