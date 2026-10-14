import { useEffect, useRef } from "react";
import { useChatStore } from "../../stores/chatStore";
import { DocumentQaMessageList } from "./DocumentQaMessageList";
import { DocumentQaInput, type DocQaFilters } from "./DocumentQaInput";
import { DocumentQaHistoryPanel } from "./DocumentQaHistoryPanel";

export function DocumentQaPanel() {
  const enterChannel = useChatStore((s) => s.enterChannel);
  const messages = useChatStore((s) => s.messages);
  const loading = useChatStore((s) => s.loading);
  const sessions = useChatStore((s) => s.sessions);
  const sessionsLoading = useChatStore((s) => s.sessionsLoading);
  const currentSessionId = useChatStore((s) => s.sessionId);
  const sendDocQa = useChatStore((s) => s.sendDocQa);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const loadSessionMessages = useChatStore((s) => s.loadSessionMessages);
  const resetSession = useChatStore((s) => s.resetSession);

  // ref 守卫与 ChatPage 同理：StrictMode 下 effect 跑两次，而 enterChannel 在首次
  // 回放时 messages 是空的（幂等条件不成立），会真的发两遍 GET /messages。
  const enteredChannelRef = useRef(false);
  useEffect(() => {
    if (enteredChannelRef.current) return;
    enteredChannelRef.current = true;
    void enterChannel("doc_qa");
    void loadSessions("doc_qa");
  }, [enterChannel, loadSessions]);

  return (
    <div style={{ display: "flex", flexDirection: "row", gap: 16 }}>
      <DocumentQaHistoryPanel
        sessions={sessions}
        currentSessionId={currentSessionId}
        loading={sessionsLoading}
        onSelect={(sid) => void loadSessionMessages(sid)}
        onNew={resetSession}
      />
      <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
        <DocumentQaMessageList messages={messages} loading={loading} />
        <DocumentQaInput
          sessionId={currentSessionId ?? ""}
          loading={loading}
          onSend={(q, f: DocQaFilters) => void sendDocQa(q, f)}
        />
      </div>
    </div>
  );
}
