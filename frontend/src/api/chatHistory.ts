// 聊天会话历史 API 模块（对齐后端 /api/v1/sessions/chat-history 系列端点）

import axios from "axios";
import { httpClient } from "./client";
import { authHeaders } from "./authHeaders";
import type {
  ChatSession,
  SessionMessagesResponse,
} from "../types/chatHistory";

const BASE = "/sessions";

// 列出有消息的聊天会话（按最后活跃时间倒序）。limit 默认 50，与后端 controller 一致
// channel 区分普通聊天（chat）、知识问答（doc_qa）、Wiki Chat（wiki_qa）会话历史；
// 不提供时后端默认 chat
export async function listChatSessions(
  limit = 50,
  offset = 0,
  channel?: "chat" | "doc_qa" | "wiki_qa",
): Promise<ChatSession[]> {
  const params: Record<string, number | string> = { limit, offset };
  if (channel !== undefined) {
    params.channel = channel;
  }
  const res = await httpClient.get<ChatSession[]>(`${BASE}/chat-history`, { params });
  return res.data;
}

// 加载某 session 的完整消息流（按时间正序）。limit 默认 200。
// tail=true 取**最新** limit 条（仍正序返回）—— 导出配图必须与 PDF 的「最后 500 轮」
// 窗口对齐，默认的「最早 limit 条」在长会话里与那个窗口完全不相交。
export async function loadSessionMessages(
  sessionId: string,
  limit = 200,
  options: { beforeId?: number; tail?: boolean } = {},
): Promise<SessionMessagesResponse> {
  const { beforeId, tail } = options;
  const res = await httpClient.get<SessionMessagesResponse>(
    `${BASE}/${sessionId}/messages`,
    {
      params: {
        limit,
        ...(beforeId !== undefined ? { before_id: beforeId } : {}),
        ...(tail ? { tail: true } : {}),
      },
    },
  );
  return res.data;
}

// 硬删除某 session 的所有数据（message + token_usage + query_state 三表）。
// 204 成功；sessionId 不存在返 404（client 抛 Error 由调用方处理）
export async function deleteSessionHistory(sessionId: string): Promise<void> {
  await httpClient.delete(`${BASE}/${sessionId}`);
}

/** 导出请求体里的一张图表位图（messageId → data:image/png;base64,...）。 */
export interface ExportChartImage {
  messageId: number;
  imagePng: string;
}

/** 导出请求体；两者都可省 ⇒ 等价于「导出整段会话、不带图」。 */
export interface ExportSessionPayload {
  messageId?: number;
  charts?: ExportChartImage[];
}

// 导出某 session 为 PDF（content-disposition 触发浏览器下载）。
// 复用 httpClient（共享拦截器），但 PDF 是 application/pdf 二进制，
// 拦截器的 ApiResponse 信封解包逻辑会把非 success 状态判错；这里直接走
// axios 原始 response，responseType="blob" 让浏览器接收二进制流。
//
// 绕开 httpClient 的代价是**连请求拦截器一起绕开** ⇒ 必须自己用 authHeaders()
// 补 Authorization：`sessions` router 是 router 级鉴权（session.py:59），没有
// Bearer 一律 403「请先登录」。这里曾手工塞 X-Tenant-Id / X-User-Id —— 后者的
// 取值来源 `httpClient.defaults.headers["X-User-Id"]` **从来就不存在**（恒
// undefined），而 X-User-* 又会被 nginx 剥掉，于是这个按钮在 real-auth 下一直 403。
//
// 0105：GET → POST。图没法塞进 GET，而留两个入口会变成两条会漂移的路径
// （一条带图一条不带）。`charts` 省略时后端行为与旧 GET 完全一致。
export async function exportSessionPdf(
  sessionId: string,
  payload: ExportSessionPayload = {},
): Promise<Blob> {
  const res = await axios.post<Blob>(
    `${httpClient.defaults.baseURL ?? ""}${BASE}/${sessionId}/export.pdf`,
    payload,
    {
      responseType: "blob",
      headers: authHeaders({ "Content-Type": "application/json" }),
      timeout: httpClient.defaults.timeout,
    },
  );
  return res.data;
}