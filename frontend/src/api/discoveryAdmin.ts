/** 本体发现管理 API（A1 发现源/快照 + G3 运行）。
 *
 * 后端（`app/api/v1/ontology_discovery.py`）：
 * - `GET   /ontology-discovery/sources`                 登录
 * - `POST  /ontology-discovery/sources`                 admin（登记授权目录）
 * - `PATCH /ontology-discovery/sources/{id}`            admin（停用/改策略）
 * - `POST  /ontology-discovery/snapshots?sourceId=`     admin（采集快照，幂等）
 * - `GET   /ontology-discovery/snapshots`               登录
 * - `GET   /ontology-discovery/snapshots/{id}`          登录（含内容）
 * - `POST  /ontology-discovery/sources/{id}/monitor`    admin（增量监测）
 * - `POST  /ontology-discovery/runs`                    admin（触发运行，202）
 * 业务数据源列表复用 `/datasources`。
 */

import { httpClient } from "./client";

const BASE = "/ontology-discovery";

export interface DiscoverySource {
  id: number;
  name: string;
  datasourceId: number;
  owner: string;
  accessPolicy: Record<string, unknown>;
  status: string;
  note: string | null;
  createdBy: string | null;
  createdTime: string;
  snapshotCount: number;
}

export interface DiscoverySourcePage {
  rows: DiscoverySource[];
  total: number;
}

export interface SnapshotSummary {
  id: number;
  sourceId: number;
  schemaHash: string;
  tableCount: number;
  columnCount: number;
  capturedAt: string;
  createdBy: string | null;
}

export interface SnapshotPage {
  rows: SnapshotSummary[];
  total: number;
}

export interface SnapshotTable {
  tableName: string;
  owner: string;
  columns: Array<{ columnName: string; dataType: string }>;
}

export interface SnapshotDetail extends SnapshotSummary {
  snapshot: { schemaVersion?: string; tables?: SnapshotTable[] };
}

export interface SnapshotCapture {
  snapshotId: number;
  sourceId: number;
  schemaHash: string;
  tableCount: number;
  columnCount: number;
  capturedAt: string;
  reused: boolean;
}

export interface DatasourceOption {
  id: number;
  name: string;
  type: string;
  isActive: boolean;
}

export interface MonitorResult {
  sourceId: number;
  snapshotId: number;
  changed: boolean;
  runId: number | null;
}

export async function listDatasources(): Promise<DatasourceOption[]> {
  const res = await httpClient.get<DatasourceOption[]>("/datasources");
  return res.data;
}

export async function listSources(params: { limit?: number; offset?: number } = {}) {
  const res = await httpClient.get<DiscoverySourcePage>(`${BASE}/sources`, {
    params: { limit: params.limit ?? 100, offset: params.offset ?? 0 },
  });
  return res.data;
}

export async function createSource(payload: {
  name: string;
  datasourceId: number;
  owner: string;
  accessPolicy?: Record<string, unknown>;
  note?: string | null;
}): Promise<DiscoverySource> {
  const res = await httpClient.post<DiscoverySource>(`${BASE}/sources`, payload);
  return res.data;
}

export async function updateSource(
  id: number,
  payload: { status?: string; owner?: string; accessPolicy?: Record<string, unknown>; note?: string | null },
): Promise<DiscoverySource> {
  const res = await httpClient.patch<DiscoverySource>(`${BASE}/sources/${id}`, payload);
  return res.data;
}

export async function captureSnapshot(sourceId: number): Promise<SnapshotCapture> {
  const res = await httpClient.post<SnapshotCapture>(`${BASE}/snapshots?sourceId=${sourceId}`);
  return res.data;
}

export async function listSnapshots(params: { sourceId?: number; limit?: number } = {}) {
  const res = await httpClient.get<SnapshotPage>(`${BASE}/snapshots`, {
    params: { sourceId: params.sourceId ?? undefined, limit: params.limit ?? 20, offset: 0 },
  });
  return res.data;
}

export async function getSnapshot(id: number): Promise<SnapshotDetail> {
  const res = await httpClient.get<SnapshotDetail>(`${BASE}/snapshots/${id}`);
  return res.data;
}

export async function monitorSource(sourceId: number): Promise<MonitorResult> {
  const res = await httpClient.post<MonitorResult>(`${BASE}/sources/${sourceId}/monitor`);
  return res.data;
}

export async function startRun(payload: {
  sourceId: number;
  snapshotId?: number | null;
  tables?: string[] | null;
  withCandidates?: boolean;
}): Promise<{ id: number; status: string; snapshotId: number | null }> {
  const res = await httpClient.post<{ id: number; status: string; snapshotId: number | null }>(
    `${BASE}/runs`,
    payload,
  );
  return res.data;
}
