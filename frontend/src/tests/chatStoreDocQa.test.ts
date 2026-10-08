import { describe, it, expect, vi, beforeEach } from "vitest";

const chatApi = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  sendMessageStream: vi.fn(),
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
    channel: "chat",
  });
}

describe("chatStore channel field", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it("resetSession assigns chat- prefix when channel=chat", () => {
    useChatStore.getState().resetSession();
    const sid = useChatStore.getState().sessionId;
    expect(sid).toMatch(/^chat-/);
  });

  it("resetSession assigns docqa- prefix when channel=doc_qa", async () => {
    await useChatStore.getState().enterChannel("doc_qa");
    useChatStore.getState().resetSession();
    const sid = useChatStore.getState().sessionId;
    expect(sid).toMatch(/^docqa-/);
  });
});
