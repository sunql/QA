import { useCallback, useEffect, useState } from "react";
import { Button, Space, Typography, message } from "antd";
import { FilePdfOutlined } from "@ant-design/icons";
import ChatPanel from "../components/chat/ChatPanel";
import MessageList from "../components/chat/MessageList";
import TermDictionaryManager from "../components/chat/TermDictionaryManager";
import ChatHistoryPanel from "../components/chat/ChatHistoryPanel";
import HistoryToggleButton from "../components/chat/HistoryToggleButton";
import { useChatStore } from "../stores/chatStore";
import { exportSessionPdf } from "../api/chatHistory";
import { downloadBlob } from "../utils/download";
import type { ChartType } from "../types/chat";
import { useTranslation } from "../i18n";

const { Title } = Typography;

function errorMessageOf(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

// 安全审查 MEDIUM-5：防止文件名注入路径分隔符 / 控制字符。
// sessionId 走 FastAPI path 校验（String(64)），理论上仅 ASCII；
// 但 chatStore 可能从 localStorage / 历史回放载入历史 id，保守 sanitize。
function safeFilenamePart(input: string): string {
  // 只保留 ASCII 字母数字 + 短横线下划线，其它替换为下划线；截断到 64 字符
  return input.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 64) || "unknown";
}

export default function ChatPage() {
  const { t } = useTranslation();
  const messages = useChatStore((s) => s.messages);
  const loading = useChatStore((s) => s.loading);
  const datasourceId = useChatStore((s) => s.datasourceId);
  const selectedModelId = useChatStore((s) => s.selectedModelId);
  const setDatasourceId = useChatStore((s) => s.setDatasourceId);
  const setSelectedModelId = useChatStore((s) => s.setSelectedModelId);
  const send = useChatStore((s) => s.sendMessage);

  // 历史面板：sessions + 折叠态 + 5 个 action
  const sessions = useChatStore((s) => s.sessions);
  const sessionsLoading = useChatStore((s) => s.sessionsLoading);
  const sessionsError = useChatStore((s) => s.sessionsError);
  const historyPanelOpen = useChatStore((s) => s.historyPanelOpen);
  const currentSessionId = useChatStore((s) => s.sessionId);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const loadSessionMessages = useChatStore((s) => s.loadSessionMessages);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const toggleHistoryPanel = useChatStore((s) => s.toggleHistoryPanel);
  const resetSession = useChatStore((s) => s.resetSession);

  // PDF 导出进行中（按钮 Spin）；单按钮与全局按钮共用同一 loading（节流）
  const [exporting, setExporting] = useState(false);

  // 面板首次展开时拉一次列表；面板收起时不重复请求
  useEffect(() => {
    if (historyPanelOpen && sessions.length === 0 && !sessionsLoading && !sessionsError) {
      void loadSessions();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyPanelOpen]);

  const handleSend = useCallback(
    (question: string, chartType: ChartType | null) => {
      // 5.6：默认走 SSE 流式端点（token 逐块渲染），非流式端点作为回退保留
      void send(question, true, chartType);
    },
    [send]
  );

  // 点击历史项 → 加载并替换当前聊天视图
  const handleSelectSession = useCallback(
    (sessionId: string) => {
      void loadSessionMessages(sessionId);
    },
    [loadSessionMessages]
  );

  // 新对话：清空当前 messages 并切到全新 sessionId
  const handleNewChat = useCallback(() => {
    resetSession();
  }, [resetSession]);

  // 导出当前会话为 PDF（全局入口）；单条问答导出共用同一函数，传入 dbMessageId
  const handleExportSession = useCallback(
    async (dbMessageId?: number) => {
      if (messages.length === 0) {
        void message.warning(t("chat.exportPdf.emptySession"));
        return;
      }
      if (exporting) return;
      setExporting(true);
      const hide = message.loading(t("chat.exportPdf.downloading"), 0);
      try {
        const blob = await exportSessionPdf(currentSessionId, dbMessageId);
        const filename = dbMessageId
          ? `qa-message-${dbMessageId}.pdf`
          : `qa-session-${safeFilenamePart(currentSessionId)}.pdf`;
        downloadBlob(blob, filename);
      } catch (error) {
        const text = errorMessageOf(error) || t("errors.unknownError");
        void message.error(t("chat.exportPdf.failed", { message: text }));
      } finally {
        hide();
        setExporting(false);
      }
    },
    [currentSessionId, exporting, messages.length, t]
  );

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "row",
        height: "calc(100vh - 160px)",
        minHeight: 360,
        position: "relative",
      }}
    >
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          minWidth: 0,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: 12,
          }}
        >
          <Title level={4} style={{ margin: 0 }}>
            {t("pages.chat")}
          </Title>
          <Space>
            <Button
              icon={<FilePdfOutlined />}
              loading={exporting}
              onClick={() => void handleExportSession()}
              aria-label={t("chat.exportPdf.fullAriaLabel")}
            >
              {t("chat.exportPdf.fullButton")}
            </Button>
            <TermDictionaryManager />
          </Space>
        </div>
        <MessageList
          messages={messages}
          loading={loading}
          exporting={exporting}
          onExportSingleTurn={(dbMessageId) => void handleExportSession(dbMessageId)}
        />
        <ChatPanel
          datasourceId={datasourceId}
          selectedModelId={selectedModelId}
          onDatasourceChange={setDatasourceId}
          onModelChange={setSelectedModelId}
          onSend={handleSend}
          loading={loading}
        />
      </div>

      {historyPanelOpen ? (
        <ChatHistoryPanel
          sessions={sessions}
          currentSessionId={currentSessionId}
          loading={sessionsLoading}
          error={sessionsError}
          onSelect={handleSelectSession}
          onDelete={(sessionId) => deleteSession(sessionId)}
          onNewChat={handleNewChat}
          onCollapse={toggleHistoryPanel}
        />
      ) : (
        <HistoryToggleButton onClick={toggleHistoryPanel} />
      )}
    </div>
  );
}