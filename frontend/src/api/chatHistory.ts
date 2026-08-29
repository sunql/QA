// 聊天会话历史 API 模块（对齐后端 /api/v1/sessions/chat-history 系列端点）

import axios from "axios";
import { httpClient } from "./client";
import type {
  ChatSession,
  SessionMessagesResponse,
} from "../types/chatHistory";

const BASE = "/sessions";

// 列出有消息的聊天会话（按最后活跃时间倒序）。limit 默认 50，与后端 controller 一致
export async function listChatSessions(limit = 50, offset = 0): Promise<ChatSession[]> {
  const res = await httpClient.get<ChatSession[]>(`${BASE}/chat-history`, {
    params: { limit, offset },
  });
  return res.data;
}

// 加载某 session 的完整消息流（按时间正序）。limit 默认 200；
// beforeId 为 cursor：取该 id 之前更早的消息（未提供则从头加载）
export async function loadSessionMessages(
  sessionId: string,
  limit = 200,
  beforeId?: number,
): Promise<SessionMessagesResponse> {
  const res = await httpClient.get<SessionMessagesResponse>(
    `${BASE}/${sessionId}/messages`,
    {
      params: { limit, ...(beforeId !== undefined ? { beforeId } : {}) },
    },
  );
  return res.data;
}

// 硬删除某 session 的所有数据（message + token_usage + query_state 三表）。
// 204 成功；sessionId 不存在返 404（client 抛 Error 由调用方处理）
export async function deleteSessionHistory(sessionId: string): Promise<void> {
  await httpClient.delete(`${BASE}/${sessionId}`);
}

// 导出某 session 为 PDF（content-disposition 触发浏览器下载）。
// 复用 httpClient（共享拦截器），但 PDF 是 application/pdf 二进制，
// 拦截器的 ApiResponse 信封解包逻辑会把非 success 状态判错；这里直接走
// axios 原始 response，responseType="blob" 让浏览器接收二进制流。
export async function exportSessionPdf(
  sessionId: string,
  messageId?: number,
): Promise<Blob> {
  const params: Record<string, number> = {};
  if (messageId !== undefined) {
    params.messageId = messageId;
  }
  const res = await axios.get<Blob>(
    `${httpClient.defaults.baseURL ?? ""}${BASE}/${sessionId}/export.pdf`,
    {
      params,
      responseType: "blob",
      headers: {
        "X-Tenant-Id": httpClient.defaults.headers["X-Tenant-Id"] as string,
        "X-User-Id": httpClient.defaults.headers["X-User-Id"] as string,
      },
      timeout: httpClient.defaults.timeout,
    },
  );
  return res.data;
}