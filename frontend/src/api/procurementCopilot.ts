/** I3 采购 Copilot 入口客户端：`POST /procurement/copilot/messages`（SSE）。
 *
 * 为什么不用 EventSource：它不支持 POST，也不能带 Authorization 头。
 * 这里用 fetch + `readSseStream`（`utils/sseStream`，跨 chunk 拼接 + 坏帧不炸流）。
 *
 * 事件契约（后端 `services/procurement_copilot`）：
 *   copilot_run   {runId, stepsUrl}
 *   copilot_step  {stepNo, kind, status, action, planSummary, toolName, answer, ...}
 *   copilot_done  {runId, status, answer, pauseCode, totalTokens, cost, stepsUrl}
 *   copilot_error {error, errorType, runId?, stepsUrl?}
 */

import { API_BASE_URL } from "../config";
import { httpClient } from "./client";
import { authHeaders } from "./authHeaders";
import { readSseStream, type SseEvent } from "../utils/sseStream";

export interface CopilotRunRef {
  runId: string;
  stepsUrl: string;
  /** 多轮会话 id（首轮由后端生成并回传） */
  conversationId?: string;
  /** true = 这一轮是对上一轮追问的回答（后端 resume 了同一个 run） */
  resumed?: boolean;
}

export interface CopilotStepPayload {
  stepNo: number;
  iterationNo: number;
  kind: string;
  status: string;
  action: string | null;
  planSummary: string | null;
  toolName: string | null;
  answer: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  /** TOOL 步骤的 observation：{"text": "<json 字符串>", "truncated": bool} */
  output?: { text?: string; truncated?: boolean } | null;
}

export interface CopilotDonePayload {
  runId: string;
  status: string;
  answer: string | null;
  pauseCode: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  totalTokens: number;
  cost: number;
  stepsUrl: string;
}

export interface CopilotErrorPayload {
  error: string;
  errorType: string;
  runId?: string;
  stepsUrl?: string;
}

export interface CopilotStreamHandlers {
  onRun?: (payload: CopilotRunRef) => void;
  onStep?: (payload: CopilotStepPayload) => void;
  onDone?: (payload: CopilotDonePayload) => void;
  onError?: (payload: CopilotErrorPayload) => void;
}

/** 会话列表项（侧栏「历史会话」用）。 */
export interface CopilotConversationSummary {
  conversationId: string;
  agentCode: string;
  modelConfigId: number;
  turnCount: number;
  lastQuestion: string | null;
  updatedAt: string;
}

/** 会话中的一轮（刷新后恢复线程用）。 */
export interface CopilotTurnRead {
  seq: number;
  question: string;
  runId: string;
  status: string;
  answer: string | null;
  pauseCode: string | null;
  createdAt: string;
}

export interface CopilotConversationDetail {
  conversationId: string;
  agentCode: string;
  modelConfigId: number;
  turns: CopilotTurnRead[];
}

/** 列出当前用户的历史会话（最近更新在前）。 */
export async function listCopilotConversations(
  limit = 20,
): Promise<CopilotConversationSummary[]> {
  const res = await httpClient.get<CopilotConversationSummary[]>(
    "/procurement/copilot/conversations",
    { params: { limit } },
  );
  return res.data;
}

/** 读取会话详情（含每轮问题/状态/答案）。 */
export async function getCopilotConversation(
  conversationId: string,
): Promise<CopilotConversationDetail> {
  const res = await httpClient.get<CopilotConversationDetail>(
    `/procurement/copilot/conversations/${encodeURIComponent(conversationId)}`,
  );
  return res.data;
}

/** 删除会话（连带轮次；运行记录保留）。 */
export async function deleteCopilotConversation(
  conversationId: string,
): Promise<void> {
  await httpClient.delete(
    `/procurement/copilot/conversations/${encodeURIComponent(conversationId)}`,
  );
}

/** 欢迎态「业务现状」卡的轻量统计（不走编排，零 LLM 成本）。 */
export interface PendingDeliverySummaryResponse {
  summary: {
    openLines: number;
    openQty: number;
    overdueLines: number;
    excludedRows: number;
  };
  bySupplier: {
    supplierCode: string;
    openLines: number;
    openQty: number;
    overdueLines: number;
  }[];
  topLines: unknown[];
}

export interface PendingDeliveryQueryFilters {
  supplierKeys?: string[];
  materialKeys?: string[];
  siteKeys?: string[];
  dateFrom?: string;
  dateTo?: string;
}

export async function getPendingDeliverySummary(
  params: PendingDeliveryQueryFilters & { datasourceId?: number; monthsBack?: number } = {},
): Promise<PendingDeliverySummaryResponse> {
  const query: Record<string, string | number> = {};
  if (params.datasourceId !== undefined) query.datasourceId = params.datasourceId;
  if (params.monthsBack !== undefined) query.monthsBack = params.monthsBack;
  // 多值参数用**逗号分隔**：axios 默认把数组序列化成 `key[]=v`，
  // 与后端（FastAPI 读取逗号分隔字符串）对不上 ⇒ 静默变成空过滤。
  for (const key of ["supplierKeys", "materialKeys", "siteKeys"] as const) {
    const values = params[key] ?? [];
    if (values.length > 0) query[key] = values.join(",");
  }
  if (params.dateFrom) query.dateFrom = params.dateFrom;
  if (params.dateTo) query.dateTo = params.dateTo;
  const res = await httpClient.get<PendingDeliverySummaryResponse>(
    "/procurement/pending-delivery/summary",
    { params: query },
  );
  return res.data;
}

/** 基地下拉选项（FACILITY 字典 ∩ 订单实际出现过的基地）。 */
export async function listPendingDeliverySites(
  datasourceId?: number,
): Promise<{ code: string; name: string }[]> {
  const res = await httpClient.get<{ code: string; name: string }[]>(
    "/procurement/pending-delivery/sites",
    { params: datasourceId !== undefined ? { datasourceId } : {} },
  );
  return res.data;
}

export interface CopilotMessageRequest {
  message: string;
  agentCode: string;
  /** **必填且后端不改派**（后端不替你选模型）。 */
  modelConfigId: number;
  /** 多轮会话 id；缺省 = 新开会话（响应里会回传新 id）。 */
  conversationId?: string;
}

/** 把后端的错误体（字符串 / pydantic 数组）压成一句可展示的话。 */
export function describeCopilotError(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body;
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const parts = detail
        .map((item) =>
          item && typeof item === "object" && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : "",
        )
        .filter(Boolean);
      if (parts.length) return parts.join("；");
    }
  }
  return fallback;
}

function dispatchEvent(event: SseEvent, handlers: CopilotStreamHandlers): void {
  switch (event.event) {
    case "copilot_run":
      handlers.onRun?.(event.data as CopilotRunRef);
      break;
    case "copilot_step":
      handlers.onStep?.(event.data as CopilotStepPayload);
      break;
    case "copilot_done":
      handlers.onDone?.(event.data as CopilotDonePayload);
      break;
    case "copilot_error":
      handlers.onError?.(event.data as CopilotErrorPayload);
      break;
    default:
      break;
  }
}

/** 发起一次 Copilot 流式对话；返回时流已结束（或被 abort）。 */
export async function streamCopilotMessage(
  payload: CopilotMessageRequest,
  handlers: CopilotStreamHandlers,
  options: { signal?: AbortSignal; fallbackError?: string } = {},
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/procurement/copilot/messages`, {
    method: "POST",
    headers: authHeaders({
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    }),
    body: JSON.stringify({ ...payload, mode: "stream" }),
    signal: options.signal,
  });

  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    throw new Error(
      describeCopilotError(body, options.fallbackError ?? `HTTP ${response.status}`),
    );
  }
  if (!response.body) {
    throw new Error(options.fallbackError ?? "服务端未返回事件流");
  }

  await readSseStream(response.body, (event) => dispatchEvent(event, handlers), {
    onParseError: () => {
      /* 单帧坏数据不打断整轮对话（见 sseStream 注释） */
    },
  });
}
