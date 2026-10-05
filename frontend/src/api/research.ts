// 研究型 Agent 入口 API 客户端（feat-research-entry Task 9）。
//
// REST 走 httpClient（拦截器注入 Authorization + X-Tenant-Id + 解包信封）；
// SSE 走裸 fetch（axios 不支持流式），鉴权用 authHeaders()（SSOT）——
// 记住 memory 教训：SSE 漏 authHeaders 会 403。
//
// SSE 帧格式（对齐 backend/api/v1/research.py 的 `_sseFrame`）：
//   event: <name>\ndata: <json>\n\n
// 事件名在 `event:` 行 —— 与 wikiChat 的解析器不同（它只取 data: 行、丢弃
// event: 行），research 的事件名本身是语义，必须解析并按名分发。心跳帧行首是
// `:`（`: ping`），按 SSE 规范跳过。

import { API_BASE_URL } from "../config";
import { httpClient } from "./client";
import { authHeaders } from "./authHeaders";
import type {
  CheckpointAction,
  CheckpointAnswer,
  ResearchEventName,
  ResearchMode,
  ResearchReport,
  ResearchReportSummary,
  ResearchSession,
  ResearchSessionDetail,
  ResearchSseEvent,
  ResearchTurnAccepted,
} from "../types/research";

const BASE = "/research";

// --- REST -------------------------------------------------------------------

export async function createResearchSession(input: {
  question: string;
  mode?: ResearchMode;
  datasourceId?: number | null;
}): Promise<ResearchSession> {
  const res = await httpClient.post<ResearchSession>(`${BASE}/sessions`, input);
  return res.data;
}

export async function listResearchSessions(): Promise<ResearchSession[]> {
  const res = await httpClient.get<ResearchSession[]>(`${BASE}/sessions`);
  return res.data;
}

export async function deleteResearchSession(sessionId: string): Promise<void> {
  await httpClient.delete(`${BASE}/sessions/${sessionId}`);
}

export async function getResearchSession(sessionId: string): Promise<ResearchSessionDetail> {
  const res = await httpClient.get<ResearchSessionDetail>(`${BASE}/sessions/${sessionId}`);
  return res.data;
}

export async function submitTurn(
  sessionId: string,
  question: string,
): Promise<ResearchTurnAccepted> {
  const res = await httpClient.post<ResearchTurnAccepted>(
    `${BASE}/sessions/${sessionId}/turns`,
    { question },
  );
  return res.data;
}

export async function answerCheckpoint(
  checkpointId: string,
  input: { action: CheckpointAction; choice?: Record<string, unknown> },
): Promise<CheckpointAnswer> {
  const res = await httpClient.post<CheckpointAnswer>(
    `${BASE}/checkpoints/${checkpointId}/answer`,
    input,
  );
  return res.data;
}

export async function getReport(sessionId: string, version?: number): Promise<ResearchReport> {
  const res = await httpClient.get<ResearchReport>(`${BASE}/sessions/${sessionId}/report`, {
    params: version ? { version } : undefined,
  });
  return res.data;
}

export async function listReports(sessionId: string): Promise<ResearchReportSummary[]> {
  const res = await httpClient.get<ResearchReportSummary[]>(
    `${BASE}/sessions/${sessionId}/reports`,
  );
  return res.data;
}

// --- SSE --------------------------------------------------------------------

const KNOWN_EVENT_NAMES: ReadonlySet<string> = new Set<string>([
  "research.connected",
  "research.intent",
  "research.esl",
  "research.checkpoint",
  "research.plan",
  "research.step.start",
  "research.step.sql",
  "research.step.data",
  "research.step.chart",
  "research.step.done",
  "research.hypothesis",
  "research.finding",
  "research.report",
  "research.done",
  "research.error",
]);

function isResearchEventName(value: string): value is ResearchEventName {
  return KNOWN_EVENT_NAMES.has(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * 订阅某会话的 research.* SSE 进度流并逐事件回调。
 * @param sessionId 会话 id（查询参数，backend 先校验归属再建流）
 * @param onEvent 每个 SSE 事件的回调（事件名在 event.name）
 * @param signal 可选中止信号（组件卸载 / 切会话时断流）
 * @param onOpen 可选：流「已建立」（fetch 返回 ok 且 body 存在）后立即回调，供调用方
 *   在提交首轮前等待流建立（后端不重放历史）。fetch 失败时不会回调（由 store 兜底）。
 */
export async function openResearchStream(
  sessionId: string,
  onEvent: (event: ResearchSseEvent) => void,
  signal?: AbortSignal,
  onOpen?: () => void,
): Promise<void> {
  // SSE 走裸 fetch（axios 不支持流式），不经 httpClient 拦截器，
  // 故用 authHeaders()（SSOT）注入 Authorization + X-Tenant-Id。
  const url = `${API_BASE_URL}${BASE}/stream?sessionId=${encodeURIComponent(sessionId)}`;
  const res = await fetch(url, {
    headers: authHeaders(),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  onOpen?.();
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = consumeFrames(buffer, onEvent);
    }
    // 冲刷解码器缓冲的尾字节：最后一个分块可能截断多字节 UTF-8 字符。
    buffer += decoder.decode();
    consumeFrames(buffer, onEvent);
  } finally {
    reader.releaseLock();
  }
}

// 按 \n\n 拆帧，未闭合的尾帧留在 buffer 里等待下一分块（处理分帧边界）。
function consumeFrames(buffer: string, onEvent: (e: ResearchSseEvent) => void): string {
  // 统一 CRLF / CR 为 LF（兼容 HTTP 标准换行与后端当前使用的 \n）。
  const normalized = buffer.replace(/\r\n|\r/g, "\n");
  let index: number;
  let remainder = normalized;
  while ((index = remainder.indexOf("\n\n")) !== -1) {
    const frame = remainder.slice(0, index);
    remainder = remainder.slice(index + 2);
    handleFrame(frame, onEvent);
  }
  return remainder;
}

function handleFrame(frame: string, onEvent: (e: ResearchSseEvent) => void): void {
  let name = "";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue; // 心跳 / 注释行：跳过
    if (line.startsWith("event:")) {
      name = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    }
  }
  if (!name || dataLines.length === 0) return;
  if (!isResearchEventName(name)) return;
  let payload: unknown;
  try {
    // 多行 data: 按 SSE 规范以 \n 连接（后端当前单行，防御性兼容）。
    payload = JSON.parse(dataLines.join("\n"));
  } catch {
    return;
  }
  if (!isRecord(payload)) return;
  onEvent({ name, payload });
}
