/** 采购 Copilot 的会话历史侧栏（与 AIChatService 的 ChatHistoryPanel 同款形态）。
 *
 * 差异只有一处：Copilot 暂无「删除会话」接口，故不提供删除动作。
 */

import { Button, Empty, List, Popconfirm, Space, Spin, Tag, Typography } from "antd";
import { DeleteOutlined, MenuFoldOutlined, PlusOutlined } from "@ant-design/icons";
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
  onSelect: (conversationId: string) => void;
  onDelete: (conversationId: string) => Promise<void> | void;
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
  onSelect,
  onDelete,
  onNewConversation,
  onCollapse,
}: ConversationHistoryPanelProps) {
  const { t } = useTranslation();

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
            aria-label={t("procurement.copilot.historyNew")}
            title={t("procurement.copilot.historyNew")}
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

      <div style={{ flex: 1, overflowY: "auto" }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 24 }}>
            <Spin />
          </div>
        ) : conversations.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t("procurement.copilot.historyEmpty")}
            style={{ marginTop: 32 }}
          />
        ) : (
          <List
            size="small"
            dataSource={conversations}
            renderItem={(item) => {
              const active = item.conversationId === currentConversationId;
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
                    <Text ellipsis style={{ maxWidth: 240 }}>
                      {itemTitle(item, t("procurement.copilot.historyUntitled"))}
                    </Text>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <Space size={6} wrap>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {dayjs(item.updatedAt).fromNow()}
                        </Text>
                        <Tag>{`${item.turnCount}`}</Tag>
                      </Space>
                      <Popconfirm
                        title={t("procurement.copilot.historyDeleteConfirm")}
                        okText={t("common.confirm")}
                        cancelText={t("common.cancel")}
                        onConfirm={(e) => {
                          e?.stopPropagation();
                          void onDelete(item.conversationId);
                        }}
                        onCancel={(e) => e?.stopPropagation()}
                      >
                        <Button
                          type="text"
                          size="small"
                          icon={<DeleteOutlined />}
                          aria-label={t("procurement.copilot.historyDeleteAria")}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Popconfirm>
                    </div>
                  </Space>
                </List.Item>
              );
            }}
          />
        )}
      </div>
    </aside>
  );
}
