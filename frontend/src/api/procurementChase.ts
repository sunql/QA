/** 催货单 API（feat/delivery-chase-closed-loop）。
 *
 * 端点（admin）：
 *   POST /procurement/chase/from-pending               Copilot 选行 → 批量创建
 *   GET  /procurement/chase/drafts                     列表
 *   GET  /procurement/chase/drafts/{id}                详情（含行 + 决策时间线）
 *   POST /procurement/chase/drafts/{id}/validate       → PENDING_APPROVAL | NEEDS_CORRECTION
 *   POST /procurement/chase/drafts/{id}/approve        → READY_TO_SEND
 *   POST /procurement/chase/drafts/{id}/reject         → REJECTED
 *   POST /procurement/chase/drafts/{id}/cancel         → CANCELLED
 *   POST /procurement/chase/drafts/{id}/send           → SENT（本期终态）
 *
 * 复用 httpClient；与 procurementRfq.ts 同形态。
 */

import { httpClient } from "./client";

const PREFIX = "/procurement/chase";

export interface ChaseLineInput {
  poNo: string;
  poLine: string;
}

export interface ChaseDraft {
  chaseDraftId: string;
  version: number;
  status: string;
  requestId: string;
  supplierEnterpriseKey: number;
  supplierCode: string;
  expectedReplyDeadline: string;
  notes: string | null;
  lineCount: number;
  createdBy: string;
  createdTime: string;
  updatedTime: string;
}

export interface ChaseLine {
  poNo: string;
  poLine: string;
  materialCode: string | null;
  materialDesc: string | null;
  orderedQty: number | null;
  receivedQty: number | null;
  openQty: number | null;
  expectedDate: string | null;
  overdueDays: number | null;
}

export interface ChaseDecision {
  decision: string;
  actor: string;
  note: string | null;
  decidedTime: string | null;
}

export interface ChaseDraftDetail {
  draft: ChaseDraft;
  lines: ChaseLine[];
  decisions: ChaseDecision[];
}

export interface ChaseFromPendingResult {
  requestId: string;
  drafts: ChaseDraft[];
  unresolvedSuppliers: string[];
  skippedLines: Array<{ poNo: string; poLine: string }>;
}

/** Copilot 选行 → 按供应商批量创建催货单（服务端回查行数据，前端只给身份对）。 */
export async function createChaseFromPending(payload: {
  lines: ChaseLineInput[];
  notes?: string;
}): Promise<ChaseFromPendingResult> {
  const res = await httpClient.post<ChaseFromPendingResult>(
    `${PREFIX}/from-pending`,
    payload,
  );
  return res.data;
}

export async function listChaseDrafts(): Promise<ChaseDraft[]> {
  const res = await httpClient.get<ChaseDraft[]>(`${PREFIX}/drafts`);
  return res.data;
}

export async function getChaseDraft(chaseDraftId: string): Promise<ChaseDraftDetail> {
  const res = await httpClient.get<ChaseDraftDetail>(
    `${PREFIX}/drafts/${encodeURIComponent(chaseDraftId)}`,
  );
  return res.data;
}

export async function validateChaseDraft(chaseDraftId: string): Promise<ChaseDraftDetail> {
  const res = await httpClient.post<ChaseDraftDetail>(
    `${PREFIX}/drafts/${encodeURIComponent(chaseDraftId)}/validate`,
  );
  return res.data;
}

export async function decideChase(
  chaseDraftId: string,
  decision: "approve" | "reject" | "cancel",
  note?: string,
): Promise<ChaseDraftDetail> {
  const res = await httpClient.post<ChaseDraftDetail>(
    `${PREFIX}/drafts/${encodeURIComponent(chaseDraftId)}/${decision}`,
    // actor 由后端从登录身份取（不收客户端值，防伪造）
    note ? { note } : {},
  );
  return res.data;
}

export async function sendChaseDraft(chaseDraftId: string): Promise<ChaseDraftDetail> {
  const res = await httpClient.post<ChaseDraftDetail>(
    `${PREFIX}/drafts/${encodeURIComponent(chaseDraftId)}/send`,
  );
  return res.data;
}
