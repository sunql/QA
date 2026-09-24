/** 站内消息铃铛（feat-dq-evaluation-report Phase 7c）。
 *
 * 30s 轮询 /api/v1/messages/unread-count；显示 badge。
 * 点击展开 MessageDropdown。
 */

import { useEffect, useState } from "react";
import { Badge, Button } from "antd";
import { BellOutlined } from "@ant-design/icons";
import { getUnreadCount } from "../api/messages";
import MessageDropdown from "./MessageDropdown";

const POLL_INTERVAL_MS = 30_000;

export default function MessageBell() {
  const [unread, setUnread] = useState<number>(0);

  useEffect(() => {
    let cancelled = false;

    async function fetchCount(): Promise<void> {
      try {
        const n = await getUnreadCount();
        if (!cancelled) setUnread(n);
      } catch {
        // 静默：轮询失败不打扰用户
      }
    }

    void fetchCount();
    const timer = window.setInterval(() => {
      void fetchCount();
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <MessageDropdown
      trigger={
        <Badge count={unread} size="small" offset={[-4, 4]}>
          <Button
            type="text"
            icon={<BellOutlined style={{ fontSize: 18 }} />}
            aria-label="站内消息"
          />
        </Badge>
      }
    />
  );
}