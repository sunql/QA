/** M4-1 询价草稿 API 客户端（后端契约：/api/v1/procurement/rfqs，M5-1/2/3）。
 *
 * 幂等语义：create 携带 `Idempotency-Key` 头——同 key 同 payload ⇒ 200 重放
 * （返回既有草稿）；异 payload ⇒ 409。key 由本模块按调用点生成（uuid）。
 */

import { httpClient } from "./client";

const BASE = "/procurement/rfqs";

export type RfqStatus =
  | "DRAFT"
  | "VALIDATING"
  | "NEEDS_CORRECTION"
  | "PENDING_APPROVAL"
  | "READY_TO_SEND"
  | "REJECTED"
  | "CANCELLED"
  | "SENDING"
  | "SENT"
  | "CLOSED";

export interface RfqLineItem {
  materialKey: number;
  quantity: number;
  unit: string;
  deliveryDate: string;
}

export interface RfqDecision {
  decision: "SUBMIT" | "APPROVE" | "REJECT" | "CANCEL";
  actor: string;
  note: string | null;
  decidedTime: string | null;
}

export interface RfqDraft {
  rfqDraftId: string;
  version: number;
  status: RfqStatus;
  requestId: string;
  deadline: string;
  notes: string | null;
  supplierKeys: number[];
  lineItems: RfqLineItem[];
  createdBy: string | null;
  decisions: RfqDecision[];
}

export interface RfqDraftCreateBody {
  requestId: string;
  deadline: string;
  supplierKeys: number[];
  lineItems: RfqLineItem[];
  notes?: string | null;
}

export async function listRfqDrafts(limit = 50): Promise<RfqDraft[]> {
  const res = await httpClient.get<RfqDraft[]>(`${BASE}/drafts`, {
    params: { limit },
  });
  return res.data;
}

export async function getRfqDraft(rfqDraftId: string): Promise<RfqDraft> {
  const res = await httpClient.get<RfqDraft>(`${BASE}/drafts/${rfqDraftId}`);
  return res.data;
}

/** 创建草稿。返回 201（新建）/ 200（幂等重放）——两者都返回草稿本体。 */
export async function createRfqDraft(
  body: RfqDraftCreateBody,
): Promise<RfqDraft> {
  const idem = crypto.randomUUID();
  const res = await httpClient.post<RfqDraft>(`${BASE}/drafts`, body, {
    headers: { "Idempotency-Key": idem },
  });
  return res.data;
}

export async function validateRfqDraft(
  rfqDraftId: string,
  expectedVersion?: number,
): Promise<RfqDraft> {
  const res = await httpClient.post<RfqDraft>(
    `${BASE}/drafts/${rfqDraftId}/validate`,
    expectedVersion != null ? { expectedVersion } : {},
  );
  return res.data;
}

export async function submitRfqApproval(
  rfqDraftId: string,
): Promise<RfqDraft> {
  const res = await httpClient.post<RfqDraft>(
    `${BASE}/drafts/${rfqDraftId}/submit-approval`,
    {},
  );
  return res.data;
}

export async function decideRfqApproval(
  rfqDraftId: string,
  decision: "APPROVE" | "REJECT",
  note?: string,
): Promise<RfqDraft> {
  const res = await httpClient.post<RfqDraft>(
    `${BASE}/drafts/${rfqDraftId}/${decision === "APPROVE" ? "approve" : "reject"}`,
    note ? { note } : {},
  );
  return res.data;
}

export async function cancelRfqDraft(
  rfqDraftId: string,
  expectedVersion?: number,
): Promise<RfqDraft> {
  const res = await httpClient.post<RfqDraft>(
    `${BASE}/drafts/${rfqDraftId}/cancel`,
    expectedVersion != null ? { expectedVersion } : {},
  );
  return res.data;
}
