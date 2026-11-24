/** 采购 Copilot 的会话历史侧栏（与 AIChatService 的 ChatHistoryPanel 同款形态）。
 *
 * 增强（2026-11-24）：搜索框、按场景分组、置顶。
 * - 置顶状态存在 **localStorage**（前端本地），避免为此加一次迁移；
 *   需要跨设备同步时再上移后端（会话表加 pinned 字段）。
 */

import { useState } from "react";
import {
  Button,
  Empty,
  Input,
  List,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  DeleteOutlined,
  MenuFoldOutlined,
  PlusOutlined,
  PushpinFilled,
  PushpinOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import "dayjs/locale/zh-cn";
import type { CopilotConversationSummary } from "../../api/procurementCopilot";
import { useTranslation } from "../../i18n";

dayjs.extend(relativeTime);
dayjs.locale("zh-cn");

const { Text } = Typography;

interface ConversationHistoryPanelProps {
  conversations: CopilotConversationSummary[];
  currentConversationId: string | undefined;
  loading: boolean;
  /** 置顶的会话 id 列表（顺序即置顶顺序）。 */
  pinnedIds: string[];
  /** agentCode → 场景显示名（分组用）。 */
  agentNames: Record<string, string>;
  onSelect: (conversationId: string) => void;
  onDelete: (conversationId: string) => Promise<void> | void;
  onTogglePin: (conversationId: string) => void;
  onNewConversation: () => void;
  onCollapse: () => void;
}

function itemTitle(item: CopilotConversationSummary, fallback: string): string {
  const question = item.lastQuestion?.trim();
  return question && question.length > 0 ? question : fallback;
}

export default function ConversationHistoryPanel({
  conversations,
  currentConversationId,
  loading,
  pinnedIds,
  agentNames,
  onSelect,
  onDelete,
  onTogglePin,
  onNewConversation,
  onCollapse,
}: ConversationHistoryPanelProps) {
  const { t } = useTranslation();
  const [keyword, setKeyword] = useState("");

  const matched = conversations.filter((item) => {
    const q = keyword.trim().toLowerCase();
    if (!q) return true;
    return (
      (item.lastQuestion ?? "").toLowerCase().includes(q) ||
      item.agentCode.toLowerCase().includes(q)
    );
  });

  const pinned = matched.filter((item) => pinnedIds.includes(item.conversationId));
  const rest = matched.filter((item) => !pinnedIds.includes(item.conversationId));

  /** 按场景分组（组内按更新时间倒序）。 */
  const grouped = rest.reduce<Record<string, CopilotConversationSummary[]>>(
    (acc, item) => {
      const key = item.agentCode;
      acc[key] = acc[key] ?? [];
      acc[key].push(item);
      return acc;
    },
    {},
  );

  const renderItem = (item: CopilotConversationSummary) => {
    const active = item.conversationId === currentConversationId;
    const isPinned = pinnedIds.includes(item.conversationId);
    return (
      <List.Item
        key={item.conversationId}
        onClick={() => onSelect(item.conversationId)}
        role="button"
        aria-current={active ? "true" : undefined}
        style={{
          cursor: "pointer",
          padding: "10px 12px",
          background: active ? "#e6f4ff" : "transparent",
          borderBottom: "1px solid #f5f5f5",
        }}
      >
        <Space direction="vertical" size={2} style={{ width: "100%" }}>
          <Space size={4} style={{ width: "100%" }}>
            <Text ellipsis style={{ flex: 1, maxWidth: 170 }}>
              {itemTitle(item, t("procurement.copilot.historyUntitled"))}
            </Text>
            <Tooltip title={t("procurement.copilot.historyPin")}>
              <Button
                type="text"
                size="small"
                aria-label={t("procurement.copilot.historyPinAria")}
                icon={isPinned ? <PushpinFilled /> : <PushpinOutlined />}
                style={{ color: isPinned ? "#1677ff" : undefined }}
                onClick={(event) => {
                  event.stopPropagation();
                  onTogglePin(item.conversationId);
                }}
              />
            </Tooltip>
            <Popconfirm
              title={t("procurement.copilot.historyDeleteConfirm")}
              okText={t("common.confirm")}
              cancelText={t("common.cancel")}
              onConfirm={(event) => {
                event?.stopPropagation();
                void onDelete(item.conversationId);
              }}
              onCancel={(event) => event?.stopPropagation()}
            >
              <Button
                type="text"
                size="small"
                icon={<DeleteOutlined />}
                aria-label={t("procurement.copilot.historyDeleteAria")}
                onClick={(event) => event.stopPropagation()}
              />
            </Popconfirm>
          </Space>
          <Space size={6} wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {dayjs(item.updatedAt).fromNow()}
            </Text>
            <Tag>{`${item.turnCount}`}</Tag>
          </Space>
        </Space>
      </List.Item>
    );
  };

  return (
    <aside
      aria-label={t("procurement.copilot.history")}
      style={{
        width: 280,
        borderLeft: "1px solid #f0f0f0",
        background: "#fafafa",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        flexShrink: 0,
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "12px 12px 8px",
          borderBottom: "1px solid #f0f0f0",
        }}
      >
        <Text strong style={{ fontSize: 14 }}>
          {t("procurement.copilot.history")}
        </Text>
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<PlusOutlined />}
            onClick={onNewConversation}
            aria-label={t("procurement.copilot.newConversation")}
            title={t("procurement.copilot.newConversation")}
          />
          <Button
            type="text"
            size="small"
            icon={<MenuFoldOutlined />}
            onClick={onCollapse}
            aria-label={t("procurement.copilot.historyCollapse")}
            title={t("procurement.copilot.historyCollapse")}
          />
        </Space>
      </header>

      <div style={{ padding: "8px 12px" }}>
        <Input.Search
          allowClear
          size="small"
          placeholder={t("procurement.copilot.historySearch")}
          aria-label={t("procurement.copilot.historySearch")}
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
        />
      </div>

      <div style={{ flex: 1, overflowY: "auto" }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 24 }}>
            <Spin />
          </div>
        ) : matched.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t("procurement.copilot.historyEmpty")}
          />
        ) : (
          <>
            {pinned.length > 0 && (
              <>
                <Text
                  type="secondary"
                  style={{ display: "block", padding: "4px 12px", fontSize: 12 }}
                >
                  {t("procurement.copilot.historyPinned")}
                </Text>
                <List
                  size="small"
                  dataSource={pinned}
                  renderItem={renderItem}
                  style={{ marginBottom: 4 }}
                />
              </>
            )}
            {Object.entries(grouped).map(([agentCode, items]) => (
              <div key={agentCode}>
                <Text
                  type="secondary"
                  style={{ display: "block", padding: "6px 12px 2px", fontSize: 12 }}
                >
                  {agentNames[agentCode] ?? agentCode}
                </Text>
                <List size="small" dataSource={items} renderItem={renderItem} />
              </div>
            ))}
          </>
        )}
      </div>
    </aside>
  );
}
