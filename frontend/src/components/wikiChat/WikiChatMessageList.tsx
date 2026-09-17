import { List, Typography } from "antd";
import type { WikiChatMessage } from "../../types/wikiChat";
import { WikiChatCitationList } from "./WikiChatCitationList";

const { Paragraph } = Typography;

interface Props {
  messages: WikiChatMessage[];
  loading: boolean;
}

/** 把 assistant content 中的 [n] 拆成 segments（点击滚动到对应引用卡片）。 */
function renderWithCitations(text: string): React.ReactNode[] {
  const parts = text.split(/(\[\d+\])/g);
  return parts.map((part, idx) => {
    const match = part.match(/^\[(\d+)\]$/);
    if (match) {
      const n = match[1];
      return (
        <sup
          key={idx}
          className="wiki-chat-citation-ref"
          data-cite={n}
          data-testid="wiki-chat-citation-ref"
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

export function WikiChatMessageList({ messages, loading }: Props) {
  return (
    <List
      dataSource={messages}
      loading={loading}
      locale={{ emptyText: " " }}
      renderItem={(msg, idx) => (
        <List.Item key={idx} style={{ display: "block" }}>
          <Paragraph style={msg.role === "assistant" ? { fontSize: 15 } : undefined}>
            <strong>{msg.role === "user" ? "我：" : "助手："}</strong>{" "}
            {msg.role === "assistant"
              ? renderWithCitations(msg.content)
              : msg.content}
          </Paragraph>
          {msg.role === "assistant" && msg.citations && msg.citations.length > 0 && (
            <WikiChatCitationList citations={msg.citations} />
          )}
        </List.Item>
      )}
    />
  );
}
