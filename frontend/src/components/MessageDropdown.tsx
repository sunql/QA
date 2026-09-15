/** 站内消息下拉列表（feat-dq-evaluation-report Phase 7c）。
 *
 * 点开铃铛后展示最近 N 条；点击条目 → 标已读 + 跳转 link_url（若有）。
 * 由 MessageBell 在 Popover 内渲染。
 */

import { useEffect, useState } from "react";
import { Badge, Button, Empty, List, Popover, Space, Tag, Typography } from "antd";
import { useNavigate } from "react-router-dom";
import { listMessages, markMessageRead } from "../api/messages";
import type { InAppMessage } from "../types/messages";

interface MessageDropdownProps {
  trigger: React.ReactNode;
}

function formatRelative(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  const min = Math.floor(diff / 60_000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return new Date(iso).toLocaleDateString();
}

export default function MessageDropdown({ trigger }: MessageDropdownProps) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<InAppMessage[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    listMessages(false, 10)
      .then((rows) => setMessages(rows))
      .catch(() => setMessages([]))
      .finally(() => setLoading(false));
  }, [open]);

  async function handleItemClick(msg: InAppMessage): Promise<void> {
    if (!msg.readAt) {
      try {
        await markMessageRead(msg.id);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === msg.id
              ? { ...m, readAt: new Date().toISOString() }
              : m,
          ),
        );
      } catch {
        // 静默：铃铛没刷新无伤大雅
      }
    }
    if (msg.linkUrl) {
      setOpen(false);
      navigate(msg.linkUrl);
    }
  }

  async function handleMarkAllRead(): Promise<void> {
    const unread = messages.filter((m) => !m.readAt);
    await Promise.all(unread.map((m) => markMessageRead(m.id).catch(() => {})));
    setMessages((prev) =>
      prev.map((m) =>
        m.readAt ? m : { ...m, readAt: new Date().toISOString() },
      ),
    );
  }

  const unreadCount = messages.filter((m) => !m.readAt).length;

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      placement="bottomRight"
      content={
        <div style={{ width: 360, maxHeight: 480, overflow: "auto" }}>
          <Space
            style={{
              width: "100%",
              justifyContent: "space-between",
              padding: "4px 8px 12px",
            }}
          >
            <Typography.Text strong>站内消息</Typography.Text>
            {unreadCount > 0 && (
              <Button size="small" type="link" onClick={handleMarkAllRead}>
                全部已读
              </Button>
            )}
          </Space>
          {loading ? (
            <div style={{ padding: 24, textAlign: "center" }}>
              <Typography.Text type="secondary">加载中…</Typography.Text>
            </div>
          ) : messages.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="暂无消息"
              style={{ padding: 24 }}
            />
          ) : (
            <List
              dataSource={messages}
              renderItem={(msg) => (
                <List.Item
                  onClick={() => {
                    void handleItemClick(msg);
                  }}
                  style={{
                    cursor: msg.linkUrl ? "pointer" : "default",
                    padding: "8px 12px",
                    background: msg.readAt ? undefined : "rgba(24,144,255,0.06)",
                  }}
                >
                  <div style={{ width: "100%" }}>
                    <Space style={{ width: "100%", justifyContent: "space-between" }}>
                      <Typography.Text strong={!msg.readAt}>
                        {msg.title}
                      </Typography.Text>
                      {!msg.readAt && <Badge status="processing" />}
                    </Space>
                    <Typography.Paragraph
                      type="secondary"
                      style={{ marginBottom: 4, fontSize: 12 }}
                      ellipsis={{ rows: 2 }}
                    >
                      {msg.body}
                    </Typography.Paragraph>
                    <Space size={4}>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {formatRelative(msg.createdAt)}
                      </Typography.Text>
                      {msg.linkUrl && <Tag color="blue">点击查看</Tag>}
                    </Space>
                  </div>
                </List.Item>
              )}
            />
          )}
        </div>
      }
    >
      {trigger}
    </Popover>
  );
}