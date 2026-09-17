import { Button, Space, Typography } from "antd";

const { Text } = Typography;

interface Props {
  onPick: (question: string) => void;
}

// 预置建议问题（空会话时的提问入口；点击即发送）
const SUGGESTIONS = [
  "厂家合作有什么门槛？",
  "供应商准入流程是怎样的？",
  "采购域有哪些主数据规则？",
  "质量异常的处理流程是什么？",
];

export function WikiChatSuggestions({ onPick }: Props) {
  return (
    <div style={{ margin: "24px 0", textAlign: "center" }} data-testid="wiki-chat-suggestions">
      <Text type="secondary">试试这样问：</Text>
      <Space wrap style={{ marginTop: 12, justifyContent: "center" }}>
        {SUGGESTIONS.map((q) => (
          <Button key={q} onClick={() => onPick(q)}>
            {q}
          </Button>
        ))}
      </Space>
    </div>
  );
}
