/** 数据质量评估报告 — 前端类型（feat-dq-evaluation-report，Phase 5）。
 *
 * 与后端 `EvaluationReportCreate/Update/Read/ListRead` / `ViolationSampleRead`
 * 对齐（camelCase 契约）。snapshot 内部键保持 snake_case（参见
 * `backend/app/services/evaluation_report_service.py::_aggregateSnapshot`）。
 *
 * ReportStatus 业务态：DRAFT / PUBLISHED；异步评估态：PENDING / RUNNING /
 * COMPLETED / FAILED（feat-dq-evaluation-report-progress，2026-09-15）。
 */

export type ReportStatus =
  | "DRAFT"
  | "PUBLISHED"
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED";

export interface EvaluationReport {
  id: number;
  name: string;
  description: string | null;
  classIds: number[];
  ruleIds: number[];
  timeWindowStart: string; // ISO
  timeWindowEnd: string;
  status: ReportStatus;
  tags: string[];
  snapshot: Record<string, unknown>; // Phase 6 强类型化
  snapshotVersion: number;
  /** 异步评估进度（feat-dq-evaluation-report-progress，2026-09-15）。
   *  旧同步报告无此字段，保持 undefined。 */
  progress?: EvaluationReportProgress | null;
  owner: string | null;
  createdBy: string;
  createdTime: string;
  updatedTime: string;
  deletedAt: string | null;
}

export interface EvaluationReportListRead {
  rows: EvaluationReport[];
  total: number;
}

export interface EvaluationReportCreate {
  name: string;
  description?: string | null;
  classIds: number[];
  ruleIds: number[];
  timeWindowStart: string;
  timeWindowEnd: string;
  tags?: string[];
  status?: ReportStatus;
}

export interface EvaluationReportUpdate {
  name?: string;
  description?: string | null;
  tags?: string[];
  status?: ReportStatus;
}

export interface ViolationSampleRead {
  id: number;
  reportId: number;
  ruleId: number;
  datasourceId: number;
  targetTable: string;
  targetColumn: string | null;
  totalViolations: number;
  sampleSize: number;
  samplePkValues: Array<Record<string, unknown>>;
  // feat-sampling-error-visible (2026-09-15)：采样失败时的异常文本（≤500 chars）。
  // null/undefined = 采样成功或 0 命中；非空 = 采样器抛错（ORA-00933 等），
  // 前端在 PK 列显式提示「采样失败: <msg>」。
  samplingError?: string | null;
  capturedAt: string;
}

export interface ListReportsQuery {
  name?: string;
  classId?: number;
  ruleId?: number;
  createdBy?: string;
  start?: string;
  end?: string;
  limit?: number;
  offset?: number;
}

// ----- Phase 8a: Compare -----

export interface DimensionDeltaRead {
  name: string;
  left: number | null;
  right: number | null;
  delta: number | null;
}

export type RuleStatusChange =
  | "IMPROVED"
  | "REGRESSED"
  | "UNCHANGED"
  | "NEW"
  | "REMOVED";

export interface RuleDeltaRead {
  ruleId: number;
  ruleCode: string;
  passRateLeft: number | null;
  passRateRight: number | null;
  delta: number | null;
  statusChange: RuleStatusChange;
}

export interface EvaluationReportCompareRead {
  leftId: number;
  rightId: number;
  overallLeft: number | null;
  overallRight: number | null;
  overallDelta: number | null;
  dimensionDeltas: DimensionDeltaRead[];
  ruleDeltas: RuleDeltaRead[];
  rulesInLeftOnly: number[];
  rulesInRightOnly: number[];
}

// ----- Phase 8b: Share -----

export interface EvaluationReportShareCreate {
  expiresInDays: number;
}

export interface EvaluationReportShareRead {
  id: number;
  reportId: number;
  shareToken: string;
  shareUrl: string | null;
  expiresAt: string;
  accessCount: number;
  createdBy: string;
  createdAt: string;
}

// ----- Phase 9: Async progress (feat-dq-evaluation-report-progress, 2026-09-15) -----

export type ProgressStage =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED";

export interface EvaluationReportProgress {
  stage: ProgressStage;
  completed: number;
  total: number;
  currentRuleId: number | null;
  currentRuleCode: string | null;
  message: string | null;
  startedAt: string | null;
  finishedAt: string | null;
}
