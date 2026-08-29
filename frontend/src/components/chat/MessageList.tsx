import { useEffect, useRef } from "react";
import { Empty, Spin } from "antd";
import MessageItem from "./MessageItem";
import type { ChatMessage } from "../../types/chat";
import { useTranslation } from "../../i18n";

interface MessageListProps {
  messages: ChatMessage[];
  loading: boolean;
  /** PDF 导出进行中（用于禁用按钮 + 透传给子项）。 */
  exporting?: boolean;
  /** 单条问答导出回调：参数是目标消息在后端 SessionMessage 表的主键 id。
   *  仅当 message.dbMessageId 已回填时 MessageItem 才会渲染入口按钮。 */
  onExportSingleTurn?: (dbMessageId: number) => void;
}

export default function MessageList({
  messages,
  loading,
  exporting = false,
  onExportSingleTurn,
}: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const { t } = useTranslation();

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    // 流式期间最后一条消息 content 持续增长，依赖其长度保证随 token 累积自动滚动到底
  }, [messages.length, messages[messages.length - 1]?.content.length]);

  if (messages.length === 0) {
    return <Empty description={t("messageList.empty")} style={{ padding: 48 }} />;
  }

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "8px 8px 0" }}>
      {messages.map((message) => (
        <MessageItem
          key={message.id}
          message={message}
          exporting={exporting}
          onExportSingleTurn={onExportSingleTurn}
        />
      ))}
      {loading ? (
        <div style={{ textAlign: "center", padding: 8 }}>
          <Spin size="small" />
        </div>
      ) : null}
      <div ref={bottomRef} />
    </div>
  );
}
