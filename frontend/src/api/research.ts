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
import { StreamChunkParser, type StreamFrame } from "../stores/stream_chunk_parser";
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
  modelId?: number | null;
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
  input: { action: CheckpointAction; choice?: Record<string, unknown>; modelId?: number },
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

/** 拼流端点 URL：无游标时**不加** `lastEventId` 参数（与旧 URL 逐字节一致）。 */
function buildStreamUrl(sessionId: string, lastEventId?: number | null): string {
  const base = `${API_BASE_URL}${BASE}/stream?sessionId=${encodeURIComponent(sessionId)}`;
  if (lastEventId === undefined || lastEventId === null) return base;
  return `${base}&lastEventId=${encodeURIComponent(String(lastEventId))}`;
}

/**
 * 订阅某会话的 research.* SSE 进度流并逐事件回调。
 * @param sessionId 会话 id（查询参数，backend 先校验归属再建流）
 * @param onEvent 每个 SSE 事件的回调（事件名在 event.name，游标在 event.id）
 * @param signal 可选中止信号（组件卸载 / 切会话时断流）
 * @param onOpen 可选：流「已建立」（fetch 返回 ok 且 body 存在）后立即回调，供调用方
 *   在提交首轮前等待流建立。fetch 失败时不会回调（由 store 兜底）。
 * @param lastEventId 可选：上次收到的最后一条事件 `seq`（Task 12）。带上后后端**先回放**
 *   该 seq 之后已落库的事件（每帧带 `id:`），断线/刷新后不再永久丢失进度。null/undefined
 *   ⇒ 不带该参数、不回放（URL 与旧行为逐字节一致）。**同一帧可能被投递两次**（后端先订阅
 *   再回放），去重是调用方按 `event.id` 的责任 —— 见 researchStore.connectStream。
 */
export async function openResearchStream(
  sessionId: string,
  onEvent: (event: ResearchSseEvent) => void,
  signal?: AbortSignal,
  onOpen?: () => void,
  lastEventId?: number | null,
): Promise<void> {
  // SSE 走裸 fetch（axios 不支持流式），不经 httpClient 拦截器，
  // 故用 authHeaders()（SSOT）注入 Authorization + X-Tenant-Id。
  const url = buildStreamUrl(sessionId, lastEventId);
  const res = await fetch(url, {
    headers: authHeaders(),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  onOpen?.();
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  // X4（1012 cross-refs spec §P0-X4）：research SSE 切帧改走共享的
  // ``StreamChunkParser``（chat 已在用）。旧 ``consumeFrames`` 在拆帧前对整
  // buffer 做 ``replace(/\r\n|\r/g, "\n")``，会：
  //   1) 把 data 内的 ``\r`` 抹成 ``\n``，跨 chunk 时伪造虚假 ``\n\n`` 边界
  //   2) 没有 EOF flush，最后一帧若不以 ``\n\n`` 结尾会被静默丢弃
  // 现在由 parser.feed 状态化切帧（保留未完成 ``\r``，避免与 ``\n`` 误切），
  // parser.flush 在 EOF 时强制吐残留。``id:`` 行 / 事件名 / 多行 data
  // / 心跳跳过的解析仍在本文件的 ``handleFrame``。
  const parser = new StreamChunkParser();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const text = decoder.decode(value, { stream: true });
      for (const frame of parser.feed(text)) {
        handleFrame(frame, onEvent);
      }
    }
    // 冲刷解码器缓冲的尾字节：最后一个分块可能截断多字节 UTF-8 字符。
    const tail = decoder.decode();
    if (tail) {
      for (const frame of parser.feed(tail)) {
        handleFrame(frame, onEvent);
      }
    }
    // EOF 守门：把 parser 残留 buffer 强制当最后一帧返回（哪怕不完整）。
    for (const frame of parser.flush()) {
      handleFrame(frame, onEvent);
    }
  } finally {
    reader.releaseLock();
  }
}

// 解析帧内的 `id:` 行（后端 `_sseFrame` 的 `id: <seq>`）。SSE 规范：同帧出现多个
// `id:` 时**最后一个生效** —— 空值 / 非整数 ⇒ 该帧 id 为 undefined（不保留更早的
// 有效值），事件照常分发。**X4 起**这部分解析下沉到 StreamChunkParser：
//   - parser 跳过 `:` 注释行（sse 规范）
//   - parser 把多行 data 用 \n 拼回 data 字符串
//   - parser 把 event 名（去尾部 \r）放到 frame.event
//   - parser 把最后一个有效 id 放到 frame.id（无 id: 行 ⇒ 字段不设）
// 本函数只做 research 特定的「事件名白名单 + JSON parse + 记录游标」三件事。
//
// **X4 注意**：parser 的 data 字段**保留** data 行末尾的 `\r`（CRLF 分割残余），
// 因为行**内部**的 \r 是 JSON 内容一部分（如 `{"x":"a\rb"}`）。但行**末尾**的
// \r 是 CR of CRLF 终止符 ⇒ JSON 解析前必须剥掉，否则 `{"x": 1\r}` 这种
// 数据会让 JSON.parse 抛 SyntaxError。
function handleFrame(frame: StreamFrame, onEvent: (e: ResearchSseEvent) => void): void {
  const name = frame.event;
  if (!name) return;
  if (!isResearchEventName(name)) return;
  // 剥掉 data 行末尾的 \r（CRLF 终止符）；行内 \r 保留（被 JSON.parse 视为
  // 字符串内的合法字符）。\n 不剥（它是 SSE 多 data 行的合法连接符）。
  const data = frame.data.endsWith("\r") ? frame.data.slice(0, -1) : frame.data;
  let payload: unknown;
  try {
    payload = JSON.parse(data);
  } catch {
    return;
  }
  if (!isRecord(payload)) return;
  // 无 `id:` 时**不写** id 键 —— 保持既有 `toEqual({name, payload})` 断言不变形。
  // SSE 规范：``id:`` 字段空 / 非整数 ⇒ id 为 undefined（重置先前有效值）。
  onEvent(frame.id === undefined ? { name, payload } : { name, payload, id: frame.id });
}
