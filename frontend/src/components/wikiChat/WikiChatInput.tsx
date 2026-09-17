import { useState } from "react";
import { Button, Input, Select, Space } from "antd";
import { SendOutlined } from "@ant-design/icons";
import type { ModelConfig } from "../../types/modelConfig";

const { TextArea } = Input;

interface Props {
  loading: boolean;
  models: ModelConfig[];
  selectedModelId: number | null;
  onModelChange: (id: number | null) => void;
  onSend: (question: string) => void;
}

export function WikiChatInput({
  loading,
  models,
  selectedModelId,
  onModelChange,
  onSend,
}: Props) {
  const [question, setQuestion] = useState("");

  const handleSend = () => {
    const trimmed = question.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setQuestion("");
  };

  // -1 = 自动路由（照 ChatPanel 语义：null ↔ -1 映射）
  const modelOptions = [
    { label: "自动路由", value: -1 },
    ...models.map((m) => ({ label: `${m.modelName} (${m.provider})`, value: m.id })),
  ];

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Select
        style={{ width: 240 }}
        placeholder="选择模型"
        aria-label="选择模型"
        value={selectedModelId ?? -1}
        onChange={(value) => onModelChange(value === -1 ? null : value)}
        options={modelOptions}
      />
      <TextArea
        placeholder="输入问题，基于企业 Wiki 语义检索并合成答案..."
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        rows={3}
        onPressEnter={(e) => {
          if (!e.shiftKey) {
            e.preventDefault();
            handleSend();
          }
        }}
      />
      <Button
        type="primary"
        icon={<SendOutlined />}
        onClick={handleSend}
        loading={loading}
      >
        发送
      </Button>
    </Space>
  );
}
