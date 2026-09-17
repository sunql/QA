import { Button, List, Popconfirm } from "antd";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";

interface SessionSummary {
  sessionId: string;
  title?: string;
  // 会话最后一问（chat-history API 返回；无 title 时作为显示文案，优于裸 sessionId）
  lastQuestion?: string | null;
  updatedAt?: string;
}

interface Props {
  sessions: SessionSummary[];
  currentSessionId: string | null;
  loading: boolean;
  onSelect: (sessionId: string) => void;
  onNew: () => void;
  // 可选：传入则渲染每项的删除按钮（Popconfirm 二次确认）；不传则不渲染
  onDelete?: (sessionId: string) => void;
}

const ACTIVE_BACKGROUND = "#e6f7ff";

export function DocumentQaHistoryPanel({
  sessions,
  currentSessionId,
  loading,
  onSelect,
  onNew,
  onDelete,
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
              actions={
                onDelete
                  ? [
                      <Popconfirm
                        key="del"
                        title="删除该会话？"
                        description="将删除该会话的全部消息记录，不可恢复。"
                        okText="删除"
                        cancelText="取消"
                        onConfirm={(e) => {
                          e?.stopPropagation();
                          onDelete(s.sessionId);
                        }}
                        onCancel={(e) => e?.stopPropagation()}
                      >
                        <Button
                          className="wiki-history-delete"
                          type="text"
                          size="small"
                          aria-label="删除"
                          icon={<DeleteOutlined />}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Popconfirm>,
                    ]
                  : undefined
              }
              style={{
                cursor: "pointer",
                background: isActive ? ACTIVE_BACKGROUND : undefined,
                padding: "8px 12px",
              }}
            >
              {s.title || s.lastQuestion || s.sessionId.slice(0, 16)}
            </List.Item>
          );
        }}
      />
    </div>
  );
}