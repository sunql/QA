/** 发现评审 API（G5 前台签收）。
 *
 * 后端契约（`app/api/v1/ontology_discovery.py`）：
 * - `GET   /ontology-discovery/runs`                       运行列表（选择器）
 * - `GET   /ontology-discovery/candidates?runId=`           候选（AI 未确认结论）
 * - `GET   /ontology-discovery/findings?runId=`             主数据问题
 * - `PATCH /ontology-discovery/candidates/{id}/decision`    采纳 / 驳回 / 待证（+ 备注）
 * - `PATCH /ontology-discovery/findings/{id}/status`        确认 / 已解决 / 忽略（+ 备注）
 *
 * 整组 **admin-only**；判定可改判，历史由 audit_log 承担。
 */

import { httpClient } from "./client";

const RUNS = "/ontology-discovery/runs";
const CANDIDATES = "/ontology-discovery/candidates";
const FINDINGS = "/ontology-discovery/findings";

export interface DiscoveryRunSummary {
  id: number;
  sourceId: number;
  snapshotId: number | null;
  trigger: string;
  status: string;
  modelVersion: string | null;
  promptVersion: string | null;
  promptTokens: number;
  completionTokens: number;
  cost: number;
  errorMessage: string | null;
  startedAt: string;
  finishedAt: string | null;
  createdBy: string | null;
}

export interface DiscoveryRunPage {
  rows: DiscoveryRunSummary[];
  total: number;
}

export interface DiscoveryEvidenceRef {
  channel?: string;
  kind?: string;
  ref?: string;
  table?: string;
  column?: string | null;
  summary?: string;
}

export interface DiscoveryCandidate {
  id: number;
  runId: number;
  sourceId: number;
  snapshotId: number;
  candidateType: string;
  tableName: string | null;
  columnName: string | null;
  targetCode: string | null;
  payload: Record<string, unknown>;
  evidenceRefs: DiscoveryEvidenceRef[];
  confidence: number | null;
  modelVersion: string | null;
  promptVersion: string | null;
  status: string;
  decidedBy: string | null;
  decidedTime: string | null;
  decisionNote: string | null;
  linkedEntityType: string | null;
  linkedEntityCode: string | null;
  createdTime: string;
}

export interface DiscoveryCandidatePage {
  rows: DiscoveryCandidate[];
  total: number;
}

export interface DiscoveryFinding {
  id: number;
  runId: number;
  sourceId: number;
  snapshotId: number | null;
  findingType: string;
  tableName: string | null;
  columnName: string | null;
  severity: string;
  status: string;
  detail: string;
  evidenceRefs: DiscoveryEvidenceRef[];
  decisionNote: string | null;
  linkedEntityType: string | null;
  linkedEntityCode: string | null;
  createdTime: string;
}

export interface DiscoveryFindingPage {
  rows: DiscoveryFinding[];
  total: number;
}

export async function listDiscoveryRuns(params: {
  sourceId?: number;
  status?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<DiscoveryRunPage> {
  const res = await httpClient.get<DiscoveryRunPage>(RUNS, {
    params: {
      sourceId: params.sourceId ?? undefined,
      status: params.status || undefined,
      limit: params.limit ?? 20,
      offset: params.offset ?? 0,
    },
  });
  return res.data;
}

export async function listCandidates(params: {
  runId: number;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<DiscoveryCandidatePage> {
  const res = await httpClient.get<DiscoveryCandidatePage>(CANDIDATES, {
    params: {
      runId: params.runId,
      status: params.status || undefined,
      limit: params.limit ?? 200,
      offset: params.offset ?? 0,
    },
  });
  return res.data;
}

export async function listFindings(params: {
  runId: number;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<DiscoveryFindingPage> {
  const res = await httpClient.get<DiscoveryFindingPage>(FINDINGS, {
    params: {
      runId: params.runId,
      status: params.status || undefined,
      limit: params.limit ?? 200,
      offset: params.offset ?? 0,
    },
  });
  return res.data;
}

export async function decideCandidate(
  id: number,
  payload: { status: string; note?: string | null; linkedEntityType?: string | null; linkedEntityCode?: string | null },
): Promise<DiscoveryCandidate> {
  const res = await httpClient.patch<DiscoveryCandidate>(
    `${CANDIDATES}/${id}/decision`,
    payload,
  );
  return res.data;
}

export async function decideFinding(
  id: number,
  payload: { status: string; note?: string | null; linkedEntityType?: string | null; linkedEntityCode?: string | null },
): Promise<DiscoveryFinding> {
  const res = await httpClient.patch<DiscoveryFinding>(
    `${FINDINGS}/${id}/status`,
    payload,
  );
  return res.data;
}
