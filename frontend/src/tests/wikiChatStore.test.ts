// WikiChatStore 测试（feat-wiki-chat）：send 流式拼装 + 错误路径 + 会话历史。

import { describe, it, expect, vi, beforeEach } from "vitest";

const wikiChatApi = vi.hoisted(() => ({
  sendWikiChat: vi.fn(),
}));
vi.mock("../api/wikiChat", () => wikiChatApi);

const historyApi = vi.hoisted(() => ({
  listChatSessions: vi.fn(),
  loadSessionMessages: vi.fn(),
  deleteSessionHistory: vi.fn(),
}));
vi.mock("../api/chatHistory", () => historyApi);

import { useWikiChatStore } from "../stores/wikiChatStore";
import type { WikiChatSseEvent } from "../types/wikiChat";

function resetStore() {
  useWikiChatStore.setState({
    messages: [],
    sessionId: "wikicha-test",
    loading: false,
    error: null,
    sessions: [],
    sessionsLoading: false,
    selectedModelId: null,
  });
}

describe("wikiChatStore.send", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it("appends user message and streams tokens into placeholder", async () => {
    wikiChatApi.sendWikiChat.mockImplementation(
      async (_payload: unknown, onEvent: (e: WikiChatSseEvent) => void) => {
        onEvent({ kind: "citations", citations: [{ id: 1, pageId: "P", title: "t", score: 0.5 }] });
        onEvent({ kind: "token", content: "根" });
        onEvent({ kind: "token", content: "据" });
        onEvent({ kind: "done", tokensUsed: 15 });
      },
    );

    await useWikiChatStore.getState().send("门槛");

    const { messages, loading, error } = useWikiChatStore.getState();
    expect(messages).toHaveLength(2);
    expect(messages[0]).toMatchObject({ role: "user", content: "门槛" });
    expect(messages[1]).toMatchObject({ role: "assistant", content: "根据" });
    expect(messages[1].citations).toHaveLength(1);
    expect(loading).toBe(false);
    expect(error).toBeNull();

    // 请求 payload 契约
    expect(wikiChatApi.sendWikiChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "wikicha-test", question: "门槛" }),
      expect.any(Function),
    );
  });

  it("passes selectedModelId as modelId in payload", async () => {
    wikiChatApi.sendWikiChat.mockImplementation(async () => undefined);
    useWikiChatStore.setState({ selectedModelId: 1 });

    await useWikiChatStore.getState().send("q");

    expect(wikiChatApi.sendWikiChat).toHaveBeenCalledWith(
      expect.objectContaining({ modelId: 1 }),
      expect.any(Function),
    );
  });

  it("omits modelId when auto-route (null)", async () => {
    wikiChatApi.sendWikiChat.mockImplementation(async () => undefined);

    await useWikiChatStore.getState().send("q");

    const payload = wikiChatApi.sendWikiChat.mock.calls[0][0];
    expect(payload.modelId).toBeUndefined();
  });

  it("records error event and still clears loading", async () => {
    wikiChatApi.sendWikiChat.mockImplementation(
      async (_p: unknown, onEvent: (e: WikiChatSseEvent) => void) => {
        onEvent({ kind: "error", error: "向量检索不可用" });
      },
    );

    await useWikiChatStore.getState().send("q");

    const { messages, loading, error } = useWikiChatStore.getState();
    expect(error).toBe("向量检索不可用");
    expect(messages[1].content).toContain("向量检索不可用");
    expect(loading).toBe(false);
  });

  it("catches thrown network errors without crashing", async () => {
    wikiChatApi.sendWikiChat.mockRejectedValue(new Error("HTTP 503"));

    await useWikiChatStore.getState().send("q");

    expect(useWikiChatStore.getState().loading).toBe(false);
    expect(useWikiChatStore.getState().error).toBe("HTTP 503");
  });
});

describe("wikiChatStore sessions", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it("resetSession assigns wikicha- prefix and clears messages", () => {
    useWikiChatStore.setState({ messages: [{ role: "user", content: "x" }] });
    useWikiChatStore.getState().resetSession();
    expect(useWikiChatStore.getState().sessionId).toMatch(/^wikicha-/);
    expect(useWikiChatStore.getState().messages).toEqual([]);
  });

  it("loadSessions fetches wiki_qa channel sessions", async () => {
    historyApi.listChatSessions.mockResolvedValue([
      { sessionId: "wikicha-1", messageCount: 2 },
    ]);
    await useWikiChatStore.getState().loadSessions();
    expect(historyApi.listChatSessions).toHaveBeenCalledWith(50, 0, "wiki_qa");
    expect(useWikiChatStore.getState().sessions).toHaveLength(1);
  });

  it("deleteSession removes it from list; current session gets reset", async () => {
    historyApi.deleteSessionHistory.mockResolvedValue(undefined);
    useWikiChatStore.setState({
      sessions: [
        { sessionId: "wc-a", firstTime: "", lastTime: "", messageCount: 2, lastQuestion: null, lastAnswerPreview: null },
        { sessionId: "wc-b", firstTime: "", lastTime: "", messageCount: 2, lastQuestion: null, lastAnswerPreview: null },
      ],
      sessionId: "wc-a",
      messages: [{ role: "user", content: "x" }],
    });

    // 删除非当前会话
    await useWikiChatStore.getState().deleteSession("wc-b");
    expect(historyApi.deleteSessionHistory).toHaveBeenCalledWith("wc-b");
    expect(useWikiChatStore.getState().sessions.map((s) => s.sessionId)).toEqual(["wc-a"]);
    // 当前会话不受影响
    expect(useWikiChatStore.getState().sessionId).toBe("wc-a");
    expect(useWikiChatStore.getState().messages).toHaveLength(1);

    // 删除当前会话 → 清空消息并换新 sessionId
    await useWikiChatStore.getState().deleteSession("wc-a");
    const { sessions, sessionId, messages } = useWikiChatStore.getState();
    expect(sessions).toEqual([]);
    expect(sessionId).toMatch(/^wikicha-/);
    expect(sessionId).not.toBe("wc-a");
    expect(messages).toEqual([]);
  });

  it("loadSessionMessages maps history rows to messages", async () => {
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "wikicha-1",
      messages: [
        { id: 1, role: "user", content: "问", question: null, sql: null, createdTime: "" },
        { id: 2, role: "assistant", content: "答", question: null, sql: null, createdTime: "" },
      ],
    });
    await useWikiChatStore.getState().loadSessionMessages("wikicha-1");
    const { messages, sessionId, loading } = useWikiChatStore.getState();
    expect(sessionId).toBe("wikicha-1");
    expect(messages).toEqual([
      { role: "user", content: "问" },
      { role: "assistant", content: "答" },
    ]);
    expect(loading).toBe(false);
  });
});
