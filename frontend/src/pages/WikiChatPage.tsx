import { useEffect, useState } from "react";
import { useWikiChatStore } from "../stores/wikiChatStore";
import { WikiChatMessageList } from "../components/wikiChat/WikiChatMessageList";
import { WikiChatInput } from "../components/wikiChat/WikiChatInput";
import { WikiChatSuggestions } from "../components/wikiChat/WikiChatSuggestions";
import { DocumentQaHistoryPanel } from "../components/documents/DocumentQaHistoryPanel";
import { listModels } from "../api/modelConfig";
import type { ModelConfig } from "../types/modelConfig";

/** Wiki Chat：基于企业 Wiki 语义检索的对话问答页（feat-wiki-chat）。
 *
 * 布局照 DocumentQaPanel：左侧历史会话（复用 props 驱动的 HistoryPanel），
 * 右侧消息列表 + 建议问题 + 输入区（含模型选择，照 ChatPanel 语义）。
 * SSE 流式渲染，答案带 [n] 引用。
 */
export default function WikiChatPage() {
  const messages = useWikiChatStore((s) => s.messages);
  const loading = useWikiChatStore((s) => s.loading);
  const sessions = useWikiChatStore((s) => s.sessions);
  const sessionsLoading = useWikiChatStore((s) => s.sessionsLoading);
  const currentSessionId = useWikiChatStore((s) => s.sessionId);
  const selectedModelId = useWikiChatStore((s) => s.selectedModelId);
  const send = useWikiChatStore((s) => s.send);
  const setSelectedModelId = useWikiChatStore((s) => s.setSelectedModelId);
  const loadSessions = useWikiChatStore((s) => s.loadSessions);
  const loadSessionMessages = useWikiChatStore((s) => s.loadSessionMessages);
  const deleteSession = useWikiChatStore((s) => s.deleteSession);
  const resetSession = useWikiChatStore((s) => s.resetSession);
  const [models, setModels] = useState<ModelConfig[]>([]);

  useEffect(() => {
    void loadSessions();
    // 模型列表加载失败不影响聊天（回退自动路由）
    let cancelled = false;
    listModels(true)
      .then((data) => {
        if (!cancelled) setModels(data);
      })
      .catch(() => {
        if (!cancelled) setModels([]);
      });
    return () => {
      cancelled = true;
    };
  }, [loadSessions]);

  // 选中的模型不在 active 列表（被禁用）时切回自动路由
  useEffect(() => {
    if (
      selectedModelId !== null &&
      models.length > 0 &&
      !models.some((m) => m.id === selectedModelId)
    ) {
      setSelectedModelId(null);
    }
  }, [models, selectedModelId, setSelectedModelId]);

  return (
    <div style={{ padding: 16 }}>
      <h2 style={{ marginTop: 0 }}>Wiki Chat</h2>
      <div style={{ display: "flex", flexDirection: "row", gap: 16 }}>
        <DocumentQaHistoryPanel
          sessions={sessions}
          currentSessionId={currentSessionId}
          loading={sessionsLoading}
          onSelect={(sid) => void loadSessionMessages(sid)}
          onNew={resetSession}
          onDelete={(sid) => void deleteSession(sid)}
        />
        <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
          {messages.length === 0 && (
            <WikiChatSuggestions onPick={(q) => void send(q)} />
          )}
          <WikiChatMessageList messages={messages} loading={loading} />
          <WikiChatInput
            loading={loading}
            models={models}
            selectedModelId={selectedModelId}
            onModelChange={setSelectedModelId}
            onSend={(q) => void send(q)}
          />
        </div>
      </div>
    </div>
  );
}
