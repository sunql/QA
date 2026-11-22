/** 语义发布管理 API（G1，发布管理前端页）。
 *
 * 后端契约（`app/api/v1/semantic_releases.py`，整组 **admin-only**）：
 * - `GET  /semantic/releases`                 列表（status / domainCode / globalOnly 过滤，分页）
 * - `GET  /semantic/releases/{id}`            详情（含 manifest 快照）
 * - `GET  /semantic/releases/{id}/checks`     校验结论（severity×result 正交）
 * - `POST /semantic/releases`                 从当前治理表编译 DRAFT
 * - `POST /semantic/releases/{id}/validate`   复核（阻断项非空 ⇒ 拒绝）
 * - `POST /semantic/releases/{id}/activate`   激活（域内单活）
 * - `POST /semantic/releases/{id}/archive`    归档
 * - `POST /semantic/releases/{id}/rollback`   副本式回滚到历史版本
 */

import { httpClient } from "./client";

const BASE = "/semantic/releases";

export interface SemanticReleaseSummary {
  id: number;
  version: string;
  domainCode: string | null;
  status: string;
  schemaVersion: string;
  manifestHash: string;
  createdBy: string | null;
  createdTime: string;
  activatedTime: string | null;
}

export interface SemanticReleasePage {
  rows: SemanticReleaseSummary[];
  total: number;
}

export interface SemanticReleaseSnapshot extends SemanticReleaseSummary {
  manifest: Record<string, unknown>;
}

export interface SemanticReleaseCheck {
  id: number;
  releaseId: number;
  checkCode: string;
  severity: string;
  result: string;
  detail: Record<string, unknown> | null;
}

export interface SemanticReleaseCheckPage {
  rows: SemanticReleaseCheck[];
  total: number;
}

export interface SemanticReleaseOperation {
  release: SemanticReleaseSummary;
  /** 创建草稿时可能非空（DRAFT 允许带阻断项落库，供评审看被拦下的内容） */
  blockers: string[];
  warnings: string[];
}

export async function listSemanticReleases(params: {
  status?: string;
  domainCode?: string;
  globalOnly?: boolean;
  limit?: number;
  offset?: number;
} = {}): Promise<SemanticReleasePage> {
  const res = await httpClient.get<SemanticReleasePage>(BASE, {
    params: {
      status: params.status || undefined,
      domainCode: params.domainCode || undefined,
      globalOnly: params.globalOnly || undefined,
      limit: params.limit ?? 50,
      offset: params.offset ?? 0,
    },
  });
  return res.data;
}

export async function getSemanticRelease(id: number): Promise<SemanticReleaseSnapshot> {
  const res = await httpClient.get<SemanticReleaseSnapshot>(`${BASE}/${id}`);
  return res.data;
}

export async function listSemanticReleaseChecks(
  id: number,
  params: { limit?: number; offset?: number } = {},
): Promise<SemanticReleaseCheckPage> {
  const res = await httpClient.get<SemanticReleaseCheckPage>(`${BASE}/${id}/checks`, {
    params: { limit: params.limit ?? 200, offset: params.offset ?? 0 },
  });
  return res.data;
}

export async function createSemanticReleaseDraft(payload: {
  version: string;
  domainCode?: string | null;
  note?: string | null;
}): Promise<SemanticReleaseOperation> {
  const res = await httpClient.post<SemanticReleaseOperation>(BASE, payload);
  return res.data;
}

export async function validateSemanticRelease(id: number): Promise<SemanticReleaseOperation> {
  const res = await httpClient.post<SemanticReleaseOperation>(`${BASE}/${id}/validate`);
  return res.data;
}

export async function activateSemanticRelease(id: number): Promise<SemanticReleaseOperation> {
  const res = await httpClient.post<SemanticReleaseOperation>(`${BASE}/${id}/activate`);
  return res.data;
}

export async function archiveSemanticRelease(id: number): Promise<SemanticReleaseOperation> {
  const res = await httpClient.post<SemanticReleaseOperation>(`${BASE}/${id}/archive`);
  return res.data;
}

export async function rollbackSemanticRelease(
  id: number,
  payload: { version: string; note?: string | null },
): Promise<SemanticReleaseOperation> {
  const res = await httpClient.post<SemanticReleaseOperation>(
    `${BASE}/${id}/rollback`,
    payload,
  );
  return res.data;
}
