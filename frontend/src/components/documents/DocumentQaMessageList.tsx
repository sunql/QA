import { List, Typography } from "antd";
import type { DocQaCitation } from "../../types/document";
import { DocumentQaCitationList } from "./DocumentQaCitationList";

const { Paragraph } = Typography;

interface Message {
  role: "user" | "assistant";
  content: string;
  citations?: DocQaCitation[] | null;
}

interface Props {
  messages: Message[];
  loading: boolean;
}

/** 把 assistant content 中的 [n] 拆成 segments，返回 React 节点。 */
function renderWithCitations(text: string): React.ReactNode[] {
  const parts = text.split(/(\[\d+\])/g);
  return parts.map((part, idx) => {
    const match = part.match(/^\[(\d+)\]$/);
    if (match) {
      const n = match[1];
      return (
        <sup
          key={idx}
          className="doc-qa-citation-ref"
          data-cite={n}
          data-testid="doc-qa-citation-ref"
          onClick={() => {
            const target = document.querySelector(`[data-citation-id="${n}"]`);
            target?.scrollIntoView({ behavior: "smooth", block: "center" });
          }}
          style={{ cursor: "pointer", color: "#00D9C0" }}
        >
          [{n}]
        </sup>
      );
    }
    return <span key={idx}>{part}</span>;
  });
}

export function DocumentQaMessageList({ messages, loading }: Props) {
  return (
    <List
      dataSource={messages}
      loading={loading}
      renderItem={(msg, idx) => (
        <List.Item key={idx} style={{ display: "block" }}>
          <Paragraph>
            <strong>{msg.role === "user" ? "我：" : "助手："}</strong>{" "}
            {msg.role === "assistant"
              ? renderWithCitations(msg.content)
              : msg.content}
          </Paragraph>
          {msg.role === "assistant" && msg.citations && msg.citations.length > 0 && (
            <DocumentQaCitationList citations={msg.citations} />
          )}
        </List.Item>
      )}
    />
  );
}
