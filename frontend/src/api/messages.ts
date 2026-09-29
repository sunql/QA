/** 站内消息 API（feat-dq-evaluation-report Phase 7c）。
 *
 * - listMessages(unreadOnly, limit) → 当前用户的消息列表（按 created_at 倒序）
 * - getUnreadCount() → 铃铛 badge
 * - markMessageRead(id) → 标已读（204）
 */

import { httpClient } from "./client";
import type { InAppMessage, InAppUnreadCount } from "../types/messages";

export async function listMessages(
  unreadOnly = false,
  limit = 20,
): Promise<InAppMessage[]> {
  const res = await httpClient.get<InAppMessage[]>("/messages", {
    params: { unreadOnly, limit },
  });
  return res.data;
}

export async function getUnreadCount(): Promise<number> {
  const res = await httpClient.get<InAppUnreadCount>(
    "/messages/unread-count",
  );
  return res.data.unreadCount;
}

export async function markMessageRead(id: number): Promise<void> {
  await httpClient.post(`/messages/${id}/read`);
}