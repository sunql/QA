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
  read: vi.fn<() => { lastSessionId: string | null; historyPanelOpen: boolean }>(
    () => ({ lastSessionId: null, historyPanelOpen: false })
  ),
  write: vi.fn<(patch: { lastSessionId?: string | null; historyPanelOpen?: boolean }) => void>(),
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

  it("resetSession assigns docqa- prefix when channel=doc_qa", () => {
    useChatStore.getState().setChannel("doc_qa");
    useChatStore.getState().resetSession();
    const sid = useChatStore.getState().sessionId;
    expect(sid).toMatch(/^docqa-/);
  });
});
