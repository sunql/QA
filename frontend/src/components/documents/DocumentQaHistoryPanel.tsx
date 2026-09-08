import { Button, List } from "antd";
import { PlusOutlined } from "@ant-design/icons";

interface SessionSummary {
  sessionId: string;
  title?: string;
  updatedAt?: string;
}

interface Props {
  sessions: SessionSummary[];
  currentSessionId: string | null;
  loading: boolean;
  onSelect: (sessionId: string) => void;
  onNew: () => void;
}

const ACTIVE_BACKGROUND = "#e6f7ff";

export function DocumentQaHistoryPanel({
  sessions,
  currentSessionId,
  loading,
  onSelect,
  onNew,
}: Props) {
  return (
    <div className="doc-qa-history-panel">
      <Button type="primary" icon={<PlusOutlined />} onClick={onNew} block>
        新对话
      </Button>
      <List
        loading={loading}
        dataSource={sessions}
        renderItem={(s) => {
          const isActive = s.sessionId === currentSessionId;
          return (
            <List.Item
              onClick={() => onSelect(s.sessionId)}
              style={{
                cursor: "pointer",
                background: isActive ? ACTIVE_BACKGROUND : undefined,
                padding: "8px 12px",
              }}
            >
              {s.title || s.sessionId.slice(0, 16)}
            </List.Item>
          );
        }}
      />
    </div>
  );
}