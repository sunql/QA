/** 站内消息相关类型（feat-dq-evaluation-report Phase 7c）。
 *
 * 后端：GET /api/v1/messages、/api/v1/messages/unread-count、POST /api/v1/messages/{id}/read。
 * 用于顶部 MessageBell 铃铛 + MessageDropdown 列表。
 */

export interface InAppMessage {
  id: number;
  recipient: string;
  title: string;
  body: string;
  linkUrl: string | null;
  createdAt: string;
  readAt: string | null;
}

export interface InAppUnreadCount {
  unreadCount: number;
}