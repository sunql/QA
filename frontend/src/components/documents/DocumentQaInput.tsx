import { useState } from "react";
import { Button, Input, Select, Space } from "antd";
import { SendOutlined } from "@ant-design/icons";

const { TextArea } = Input;

export interface DocQaFilters {
  securityLevel?: string;
  documentType?: string;
}

interface Props {
  sessionId: string;
  loading: boolean;
  onSend: (question: string, filters: DocQaFilters) => void;
}

const DOCUMENT_TYPE_OPTIONS = [
  { value: "CONTRACT", label: "合同" },
  { value: "POLICY", label: "制度" },
  { value: "GUIDE", label: "指南" },
  { value: "OTHER", label: "其他" },
];

const SECURITY_LEVEL_OPTIONS = [
  { value: "L1", label: "L1 公开" },
  { value: "L2", label: "L2 内部" },
  { value: "L3", label: "L3 机密" },
];

export function DocumentQaInput({ loading, onSend }: Props) {
  const [question, setQuestion] = useState("");
  const [securityLevel, setSecurityLevel] = useState<string | undefined>();
  const [documentType, setDocumentType] = useState<string | undefined>();

  const handleSend = () => {
    const trimmed = question.trim();
    if (!trimmed) return;
    onSend(trimmed, { securityLevel, documentType });
    setQuestion("");
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Space wrap>
        <Select
          placeholder="文档类型（可选）"
          allowClear
          style={{ width: 200 }}
          value={documentType}
          onChange={setDocumentType}
          options={DOCUMENT_TYPE_OPTIONS}
        />
        <Select
          placeholder="安全级别（可选）"
          allowClear
          style={{ width: 160 }}
          value={securityLevel}
          onChange={setSecurityLevel}
          options={SECURITY_LEVEL_OPTIONS}
        />
      </Space>
      <TextArea
        placeholder="输入问题，从已上传文档中检索并合成答案..."
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