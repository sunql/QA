import {
  Alert,
  Button,
  Empty,
  List,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  DeleteOutlined,
  MenuFoldOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import "dayjs/locale/zh-cn";
import type { ChatSession } from "../../types/chatHistory";
import { useTranslation } from "../../i18n";

dayjs.extend(relativeTime);
dayjs.locale("zh-cn");

const { Text } = Typography;

interface ChatHistoryPanelProps {
  sessions: ChatSession[];
  currentSessionId: string | null;
  loading: boolean;
  error: string | null;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => Promise<void> | void;
  onNewChat: () => void;
  onCollapse: () => void;
}

export default function ChatHistoryPanel({
  sessions,
  currentSessionId,
  loading,
  error,
  onSelect,
  onDelete,
  onNewChat,
  onCollapse,
}: ChatHistoryPanelProps) {
  const { t } = useTranslation();

  return (
    <aside
      aria-label={t("chat.history.title")}
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
          {t("chat.history.title")}
        </Text>
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<PlusOutlined />}
            onClick={onNewChat}
            aria-label={t("chat.history.newChat")}
          >
            {t("chat.history.newChat")}
          </Button>
          <Button
            type="text"
            size="small"
            icon={<MenuFoldOutlined />}
            onClick={onCollapse}
            aria-label={t("chat.history.collapse")}
          />
        </Space>
      </header>

      <div style={{ flex: 1, overflowY: "auto", padding: "8px 0" }}>
        {loading ? (
          <div
            style={{
              display: "flex",
              justifyContent: "center",
              padding: 24,
            }}
          >
            <Spin />
          </div>
        ) : error ? (
          <Alert
            type="error"
            message={error}
            style={{ margin: "8px 12px" }}
            showIcon
          />
        ) : sessions.length === 0 ? (
          <Empty
            description={t("chat.history.empty")}
            style={{ marginTop: 32 }}
          />
        ) : (
          <List
            dataSource={sessions}
            renderItem={(session) => {
              const isActive = session.sessionId === currentSessionId;
              const displayTitle =
                session.lastQuestion ?? t("chat.history.untitledQuestion");
              return (
                <List.Item
                  key={session.sessionId}
                  onClick={() => onSelect(session.sessionId)}
                  style={{
                    cursor: "pointer",
                    padding: "8px 12px",
                    borderBottom: "1px solid #f0f0f0",
                    background: isActive ? "#e6f4ff" : "transparent",
                    borderLeft: isActive
                      ? "3px solid #1677ff"
                      : "3px solid transparent",
                    transition: "background 0.15s",
                  }}
                >
                  <div style={{ width: "100%" }}>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 8,
                      }}
                    >
                      <Text
                        ellipsis={{ tooltip: displayTitle }}
                        style={{ flex: 1 }}
                      >
                        {displayTitle}
                      </Text>
                      <Popconfirm
                        title={t("chat.history.deleteConfirm")}
                        okText={t("common.confirm")}
                        cancelText={t("common.cancel")}
                        onConfirm={(e) => {
                          e?.stopPropagation();
                          void onDelete(session.sessionId);
                        }}
                        onCancel={(e) => e?.stopPropagation()}
                      >
                        <Button
                          type="text"
                          size="small"
                          icon={<DeleteOutlined />}
                          aria-label={t("chat.history.deleteAriaLabel")}
                          // 阻止冒泡到 List.Item onClick
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Popconfirm>
                    </div>
                    <Space
                      size={4}
                      style={{ marginTop: 4, fontSize: 12 }}
                      wrap
                    >
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {dayjs(session.lastTime).fromNow()}
                      </Text>
                      <Tag style={{ margin: 0 }}>
                        {t("chat.history.messageCount", {
                          count: session.messageCount,
                        })}
                      </Tag>
                      {isActive ? (
                        <Tag color="blue" style={{ margin: 0 }}>
                          {t("chat.history.currentBadge")}
                        </Tag>
                      ) : null}
                    </Space>
                  </div>
                </List.Item>
              );
            }}
          />
        )}
      </div>
    </aside>
  );
}