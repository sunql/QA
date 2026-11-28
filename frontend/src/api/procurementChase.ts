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
 *   GET  /procurement/chase/drafts/{id}/export?format= 单张下载（xlsx/docx）
 *
 * 复用 httpClient；与 procurementRfq.ts 同形态。
 */

import { i18n } from "../i18n";
import { downloadBlob } from "../utils/download";
import type { AxiosResponse } from "axios";
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
  /** 欠交量异常（≥ 1,000,000）⇒ 红色标识，要求重点核对（多半是源侧录入错误）。 */
  isDirty: boolean;
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

/** 催货单导出格式：Excel 明细表（xlsx）/ Word 催货函（docx）。 */
export type ChaseExportFormat = "xlsx" | "docx";

/**
 * 由前端自己解析错误体的客户端错误状态。
 *
 * 400 ⇒ `HTTPException` ⇒ `{"detail": "..."}`；
 * 403 ⇒ `requireAdmin` 抛 `PermissionDenied`（`HTTPException` 子类）⇒
 * `{"detail": {"code": "...", "message": "..."}}`（**嵌套**）；
 * 404 ⇒ 统一错误包络 ⇒ `{"success": false, "error": "..."}`。
 * 三种形状并存（Task 2 复审 + 2026-11-28 终审）⇒ 都要能读出文案。
 */
const EXPORT_ERROR_STATUSES: readonly number[] = [400, 403, 404];

/**
 * 下载催货单（Excel 明细表 / Word 催货函）——任何状态都可下载。
 *
 * **必须走 blob 响应**：httpClient 走 Bearer token，`<a download>` 带不了
 * header ⇒ 只能先取回二进制，再转 objectURL 触发浏览器下载。
 *
 * **错误体也是 blob**（responseType 对错误响应同样生效）⇒ axios 默认会把
 * 4xx/5xx 判成 reject，而拦截器抛出的错误已经把原始 response 丢了
 * （normalizeError 只留 code/status/detail）⇒ 错误文案读不出来。
 * 因此这里让 400/403/404 **resolve**，由本函数读 blob 文本并兼容两种包络；
 * 其它状态（尤其 401）仍 reject ⇒ 拦截器的 401 清会话逻辑不被绕过。
 */
export async function downloadChaseDraft(
  chaseDraftId: string,
  format: ChaseExportFormat,
): Promise<void> {
  const res = await requestExport(chaseDraftId, format);
  const status = typeof res.status === "number" ? res.status : 200;
  if (status >= 400) {
    throw new Error(await readErrorMessage(res.data));
  }
  const blob = res.data instanceof Blob ? res.data : new Blob([String(res.data)]);
  downloadBlob(blob, resolveFilename(res.headers, chaseDraftId, format));
}

/** 发起导出请求；401 / 5xx / 网络异常 ⇒ 统一兜底文案（错误体已不可读，见上）。 */
async function requestExport(
  chaseDraftId: string,
  format: ChaseExportFormat,
): Promise<AxiosResponse<Blob>> {
  try {
    return await httpClient.get<Blob>(
      `${PREFIX}/drafts/${encodeURIComponent(chaseDraftId)}/export`,
      {
        params: { format },
        responseType: "blob",
        validateStatus: (status) =>
          (status >= 200 && status < 300) || EXPORT_ERROR_STATUSES.includes(status),
        // 错误由本函数抛「带服务端文案」的 Error 给页面提示 ⇒ 拦截器别再弹通用 toast
        silentErrors: true,
      },
    );
  } catch {
    // 拦截器抛出的错误已经丢掉原始 response（normalizeError 只留 code/status/detail）
    // ⇒ 读不到服务端文案 ⇒ 通用兜底（页面会弹提示，不会静默）
    throw new Error(i18n.t("chase.export.failed"));
  }
}

/** 从错误响应体里取可读文案；取不到 ⇒ 通用兜底文案。 */
async function readErrorMessage(payload: unknown): Promise<string> {
  if (payload && typeof payload === "object" && !(payload instanceof Blob)) {
    return pickMessage(payload) ?? i18n.t("chase.export.failed");
  }
  const parsed = parseJson(await readBlobText(payload));
  return pickMessage(parsed) ?? i18n.t("chase.export.failed");
}

/**
 * 兼容三种错误包络：
 * - `{"detail": "..."}`（400 `HTTPException`）；
 * - `{"detail": {"code": "...", "message": "..."}}`（403 `PermissionDenied`）；
 * - `{"success": false, "error": "..."}`（404 统一包络）。
 */
function pickMessage(value: unknown): string | null {
  if (typeof value === "string") return value.trim() || null;
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const detail = record.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (detail && typeof detail === "object") {
    const nested = (detail as Record<string, unknown>).message;
    if (typeof nested === "string" && nested.trim()) return nested.trim();
  }
  const error = record.error;
  if (typeof error === "string" && error.trim()) return error.trim();
  return null;
}

/** 解析 JSON；不是合法 JSON ⇒ null（不抛）。 */
function parseJson(text: string): unknown {
  if (!text.trim()) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

/**
 * 读 blob 文本。
 *
 * 浏览器有 `Blob.text()`；**jsdom 没有**（只有 FileReader）⇒ 单测里走
 * FileReader 兜底分支，否则错误文案在测试里永远是空串。
 */
function readBlobText(payload: unknown): Promise<string> {
  if (typeof payload === "string") return Promise.resolve(payload);
  if (!(payload instanceof Blob)) return Promise.resolve("");
  const withText = payload as Blob & { text?: () => Promise<string> };
  if (typeof withText.text === "function") {
    return withText.text().catch(() => "");
  }
  return new Promise<string>((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => resolve("");
    reader.readAsText(payload, "utf-8");
  });
}

/**
 * 下载文件名：优先用服务端的 `Content-Disposition`
 * （RFC 5987 的 `filename*=UTF-8''…` 优先于 ASCII 回退名）；
 * 取不到 ⇒ 本地按催货单 id 前 8 位拼一个（与后端命名口径一致）。
 */
function resolveFilename(
  headers: unknown,
  chaseDraftId: string,
  format: ChaseExportFormat,
): string {
  const disposition = readHeader(headers, "content-disposition");
  const utf8Name = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Name) {
    try {
      return decodeURIComponent(utf8Name[1]);
    } catch {
      // 百分号编码坏了 ⇒ 落回 ASCII 名
    }
  }
  const plainName = disposition.match(/filename="([^"]+)"/i);
  if (plainName) return plainName[1];
  return `催货单_${chaseDraftId.slice(0, 8)}.${format}`;
}

/** 读响应头（axios 的 AxiosHeaders 或普通对象都兼容）。 */
function readHeader(headers: unknown, name: string): string {
  if (!headers || typeof headers !== "object") return "";
  const getter = (headers as { get?: (key: string) => unknown }).get;
  if (typeof getter === "function") {
    const value = getter.call(headers, name);
    return typeof value === "string" ? value : "";
  }
  const record = headers as Record<string, unknown>;
  const value = record[name] ?? record[name.toLowerCase()];
  return typeof value === "string" ? value : "";
}
