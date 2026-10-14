import { describe, it, expect, vi, beforeEach } from "vitest";

const chatApi = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  sendMessageStream: vi.fn(),
  resumeMultiStepRun: vi.fn(),
  fetchHypotheses: vi.fn(),
}));
vi.mock("../api/chat", () => chatApi);

const historyApi = vi.hoisted(() => ({
  listChatSessions: vi.fn(),
  loadSessionMessages: vi.fn(),
  deleteSessionHistory: vi.fn(),
}));
vi.mock("../api/chatHistory", () => historyApi);

const persist = vi.hoisted(() => ({
  readLastSessionId: vi.fn<(channel: string) => string | null>(() => null),
  writeLastSessionId: vi.fn<(channel: string, sessionId: string | null) => void>(),
  readLastChannel: vi.fn<() => string | null>(() => null),
  writeLastChannel: vi.fn<(channel: string) => void>(),
  readHistoryPanelOpen: vi.fn<() => boolean>(() => false),
  writeHistoryPanelOpen: vi.fn<(open: boolean) => void>(),
}));
vi.mock("../stores/persistChatUiState", () => persist);

import { useChatStore } from "../stores/chatStore";

function resetStore() {
  useChatStore.setState({
    messages: [],
    sessionId: "s-test",
    loading: false,
    datasourceId: null,
    error: null,
    sessions: [],
    sessionsLoading: false,
    sessionsError: null,
    historyPanelOpen: false,
  });
}

describe("F2 kpiDatasourceCode", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  describe("store 层", () => {
    it("meta 帧带 datasourceMismatch=true 时 onMetaWarning 写入 kpiDatasourceCode", async () => {
      chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
        handlers.onMeta?.("query");
        handlers.onMetaWarning?.("KPI_DATASOURCE_MISMATCH", "后端原始中文提示");
        handlers.onDone?.({ tokensUsed: 10, cost: 0 });
      });
      useChatStore.getState().setDatasourceId(1);
      await useChatStore.getState().sendMessage("供应商汇总", true);

      const assistant = useChatStore.getState().messages[1];
      expect(assistant.kpiDatasourceCode).toBe("KPI_DATASOURCE_MISMATCH");
    });

    it("meta 帧无 datasourceMismatch 时 kpiDatasourceCode 不写入", async () => {
      chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
        handlers.onMeta?.("query");
        // 不调用 onMetaWarning
        handlers.onDone?.({ tokensUsed: 10, cost: 0 });
      });
      useChatStore.getState().setDatasourceId(1);
      await useChatStore.getState().sendMessage("供应商汇总", true);

      const assistant = useChatStore.getState().messages[1];
      expect(assistant.kpiDatasourceCode).toBeUndefined();
    });

    it("resumeRun 开始时清除 kpiDatasourceCode（避免上一轮警告残留）", async () => {
      // 先发一条带警告的消息
      const msgId = "msg-resume-test";
      useChatStore.setState({
        messages: [
          { id: "u1", role: "user" as const, content: "问", timestamp: 1 },
          { id: msgId, role: "assistant" as const, content: "答", timestamp: 2, kpiDatasourceCode: "KPI_DATASOURCE_MISMATCH" },
        ],
      });

      chatApi.resumeMultiStepRun.mockImplementation(async (_runId, _fromStep, _handlers) => {
        // 模拟续跑完成
      });

      await useChatStore.getState().resumeRun("run-1", 0, msgId);

      // resumeRun 会在开跑前清掉 kpiDatasourceCode
      const msg = useChatStore.getState().messages.find((m) => m.id === msgId);
      expect(msg?.kpiDatasourceCode).toBeNull();
    });
  });

  describe("API 层", () => {
    it("meta 帧带 datasourceMismatch=true 时 onMetaWarning 被调用并传入 code（不含中文原文）", async () => {
      chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
        if (handlers.onMetaWarning) {
          handlers.onMetaWarning("KPI_DATASOURCE_MISMATCH", "后端中文原文");
        }
        handlers.onDone?.({ tokensUsed: 5, cost: 0 });
      });

      useChatStore.setState({ datasourceId: 1 });
      await useChatStore.getState().sendMessage("test", true);

      const assistant = useChatStore.getState().messages[1];
      // store 只存 code，不存后端中文原文
      expect(assistant.kpiDatasourceCode).toBe("KPI_DATASOURCE_MISMATCH");
    });
  });
});
