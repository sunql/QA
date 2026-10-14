import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ChatMessageRead } from "../types/chatHistory";

// 「刷新恢复上次会话」的专门测试文件。
//
// 为什么不写进 chatStore.test.ts：模块级 hydration 只在 import 时跑一次，要验证它必须
// `vi.resetModules()` + 动态 import —— 那会换掉该文件后续用例手上的 store 实例。分文件
// 是唯一能既真测 hydration 又不污染邻居的做法（原先 chatStore.test.ts 里两个用例声称
// 测了这条路径，实际只是手工调 setState 模拟，正是缺陷长期隐身的原因）。

const chatApi = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  sendMessageStream: vi.fn(),
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

/** 取一个**刚完成模块级 hydration** 的 store 实例（每个用例一份，互不共享状态）。 */
async function bootStore() {
  vi.resetModules();
  const mod = await import("../stores/chatStore");
  return mod.useChatStore;
}

function readMsg(over: Partial<ChatMessageRead> = {}): ChatMessageRead {
  return {
    id: 1,
    role: "assistant",
    content: "回答",
    question: null,
    sql: null,
    createdTime: "2026-09-30T10:00:00Z",
    interrupted: false,
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  // 每个用例都从「干净浏览器」起：无指针、无渠道偏好、无面板状态
  persist.readLastSessionId.mockImplementation(() => null);
  persist.readLastChannel.mockImplementation(() => null);
  persist.readHistoryPanelOpen.mockImplementation(() => false);
  historyApi.loadSessionMessages.mockResolvedValue({ sessionId: "s", messages: [] });
  chatApi.sendMessage.mockResolvedValue({ answer: "ok" });
  chatApi.fetchHypotheses.mockResolvedValue({ hypotheses: [] });
});

describe("刷新恢复：模块级 hydration", () => {
  it("初始化把上次的渠道与会话指针摆好，但**不**在 import 期拉消息", async () => {
    persist.readLastChannel.mockReturnValue("chat");
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-last" : null));
    persist.readHistoryPanelOpen.mockReturnValue(true);

    const store = await bootStore();

    expect(store.getState().channel).toBe("chat");
    expect(store.getState().sessionId).toBe("chat-last");
    expect(store.getState().historyPanelOpen).toBe(true);
    // 拉消息要等面板声明渠道（enterChannel），import 期不发请求
    expect(historyApi.loadSessionMessages).not.toHaveBeenCalled();
  });

  it("没有指针时起一个全新会话（且不去请求任何会话）", async () => {
    const store = await bootStore();

    expect(store.getState().sessionId).toMatch(/^s-/);
    await store.getState().enterChannel("chat");
    expect(historyApi.loadSessionMessages).not.toHaveBeenCalled();
  });

  it("同渠道没有指针时保留当前 id（不在每次挂载时无谓地换个新会话）", async () => {
    const store = await bootStore();
    store.setState({ sessionId: "chat-keep" });

    await store.getState().enterChannel("chat");

    expect(store.getState().sessionId).toBe("chat-keep");
    expect(historyApi.loadSessionMessages).not.toHaveBeenCalled();
  });
});

describe("刷新恢复：enterChannel 回放", () => {
  it("有指针 → 用 tail 取最新那批并填进 messages", async () => {
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-last" : null));
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "chat-last",
      messages: [readMsg({ id: 7, role: "user", content: "各采购组织的收货数量" })],
    });
    const store = await bootStore();

    await store.getState().enterChannel("chat");

    // tail 是必须的：默认取的是会话**开头**，长会话会恢复出开头、中间断掉
    expect(historyApi.loadSessionMessages).toHaveBeenCalledWith("chat-last", undefined, {
      tail: true,
    });
    expect(store.getState().sessionId).toBe("chat-last");
    expect(store.getState().messages).toHaveLength(1);
    expect(store.getState().messages[0].content).toBe("各采购组织的收货数量");
  });

  it("回放出来的消息带上 dbMessageId（单条导出按钮的渲染条件）", async () => {
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-last" : null));
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "chat-last",
      messages: [readMsg({ id: 42 })],
    });
    const store = await bootStore();

    await store.getState().enterChannel("chat");

    expect(store.getState().messages[0].dbMessageId).toBe(42);
  });

  it("回放失败 → 清指针 + 换干净会话（否则刷新会反复撞同一个失败）", async () => {
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-last" : null));
    historyApi.loadSessionMessages.mockRejectedValue(new Error("404 找不到会话"));
    const store = await bootStore();

    await store.getState().enterChannel("chat");

    expect(persist.writeLastSessionId).toHaveBeenCalledWith("chat", null);
    const { sessionId, messages, sessionsError } = store.getState();
    expect(sessionId).toMatch(/^chat-/);
    expect(sessionId).not.toBe("chat-last");
    expect(messages).toHaveLength(0);
    // 失败原因仍记录（历史面板展开时可见），只是不停留在指针上
    expect(sessionsError).toBe("404 找不到会话");
  });

  it("回放 403（会话不属于当前用户）同样清指针并换会话", async () => {
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-alice" : null));
    historyApi.loadSessionMessages.mockRejectedValue(new Error("403 无权访问该会话的分析假设"));
    const store = await bootStore();

    await store.getState().enterChannel("chat");

    expect(persist.writeLastSessionId).toHaveBeenCalledWith("chat", null);
    expect(store.getState().sessionId).not.toBe("chat-alice");
    expect(store.getState().messages).toHaveLength(0);
  });

  it("同渠道重入（面板重挂载 / 路由来回切）不重复拉取", async () => {
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-last" : null));
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "chat-last",
      messages: [readMsg({ id: 1 })],
    });
    const store = await bootStore();

    await store.getState().enterChannel("chat");
    await store.getState().enterChannel("chat");

    expect(historyApi.loadSessionMessages).toHaveBeenCalledTimes(1);
    expect(store.getState().messages).toHaveLength(1);
  });

  it("换渠道 → 清空消息并取**目标渠道自己的**指针（chat 的指针不动）", async () => {
    persist.readLastSessionId.mockImplementation((ch) =>
      ch === "chat" ? "chat-last" : "docqa-last"
    );
    historyApi.loadSessionMessages.mockImplementation((sessionId: string) =>
      Promise.resolve({ sessionId, messages: [readMsg({ id: 1, content: sessionId })] })
    );
    const store = await bootStore();
    await store.getState().enterChannel("chat");

    await store.getState().enterChannel("doc_qa");

    expect(store.getState().channel).toBe("doc_qa");
    expect(store.getState().sessionId).toBe("docqa-last");
    expect(store.getState().messages[0].content).toBe("docqa-last");
    // 切渠道只声明自己，不该动另一个渠道的恢复目标
    expect(persist.writeLastSessionId).not.toHaveBeenCalledWith("chat", null);
    expect(persist.writeLastChannel).toHaveBeenCalledWith("doc_qa");
  });

  it("换渠道时清空上一个渠道的消息（跨渠道串台的既有缺陷）", async () => {
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "chat-last",
      messages: [readMsg({ id: 1, content: "chat 的回答" })],
    });
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-last" : null));
    const store = await bootStore();
    await store.getState().enterChannel("chat");
    expect(store.getState().messages).toHaveLength(1);

    await store.getState().enterChannel("doc_qa");

    expect(store.getState().messages).toHaveLength(0);
  });
});

describe("回放竞态：晚到的响应不得覆盖当前状态", () => {
  /** 手控 resolve 的 loadSessionMessages：按 sessionId 排队，用例决定谁先回来。 */
  function deferredLoads() {
    const pending = new Map<string, (value: unknown) => void>();
    historyApi.loadSessionMessages.mockImplementation(
      (sessionId: string) =>
        new Promise((resolve) => {
          pending.set(sessionId, resolve);
        })
    );
    return pending;
  }

  it("回放未回来时切走渠道 → 旧响应被丢弃（不串台、不改会话）", async () => {
    const pending = deferredLoads();
    persist.readLastSessionId.mockImplementation((ch) =>
      ch === "chat" ? "chat-last" : "docqa-last"
    );
    const store = await bootStore();

    const chatReplay = store.getState().enterChannel("chat");
    const docReplay = store.getState().enterChannel("doc_qa");
    // 让**先发**的 chat 回放在切渠道之后才回来 —— 这正是「晚到响应」的场景
    pending.get("chat-last")?.({ sessionId: "chat-last", messages: [readMsg({ id: 1, content: "chat 的回答" })] });
    await chatReplay;

    expect(store.getState().channel).toBe("doc_qa");
    expect(store.getState().sessionId).toBe("docqa-last");
    expect(store.getState().messages).toHaveLength(0);
    // 被丢弃的回放也不该认领指针（否则下个渠道的恢复目标被上一次的覆盖）
    expect(persist.writeLastSessionId).not.toHaveBeenCalledWith("chat", "chat-last");

    pending.get("docqa-last")?.({ sessionId: "docqa-last", messages: [readMsg({ id: 2, content: "doc 的回答" })] });
    await docReplay;
    expect(store.getState().messages[0].content).toBe("doc 的回答");
  });

  it("发送新消息时在途回放被作废（不冲掉用户消息与流式占位）", async () => {
    const pending = deferredLoads();
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-last" : null));
    const store = await bootStore();
    const replay = store.getState().enterChannel("chat");

    store.setState({ datasourceId: 7 });
    const send = store.getState().sendMessage("各采购组织的收货数量");
    pending.get("chat-last")?.({ sessionId: "chat-last", messages: [readMsg({ id: 1, content: "上一场的回答" })] });
    await replay;
    await send;

    const contents = store.getState().messages.map((m) => m.content);
    expect(contents).toContain("各采购组织的收货数量");
    expect(contents).not.toContain("上一场的回答");
  });
});

describe("换人：清内存里的会话（只清 localStorage 不够）", () => {
  it("换人信号到达后 messages 清空、sessionId 换新（渠道不动，指针由 authStore 清）", async () => {
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "chat-alice",
      messages: [readMsg({ id: 1, content: "alice 的对话" })],
    });
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-alice" : null));
    const store = await bootStore();
    // 必须在 bootStore 之后取：bootStore 里的 resetModules 会重建模块注册表，
    // 换人信号必须来自 store 实际注册的那一份模块实例。
    const { notifyUserSwitch } = await import("../stores/userSwitch");
    await store.getState().enterChannel("chat");
    expect(store.getState().messages).toHaveLength(1);

    notifyUserSwitch();

    // 不清的话：下一个人在同一个 tab 登录时，enterChannel 的「同渠道且已有消息」
    // 会直接早退 —— 屏幕上留着上一场对话，且接着发问会带着上一个会话的 id。
    expect(store.getState().messages).toHaveLength(0);
    expect(store.getState().sessionId).not.toBe("chat-alice");
    expect(store.getState().sessionId).toMatch(/^chat-/);
    expect(store.getState().channel).toBe("chat");
  });

  it("换人后 enterChannel 不会把那场对话恢复回来", async () => {
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-alice" : null));
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "chat-alice",
      messages: [readMsg({ id: 1, content: "alice 的对话" })],
    });
    const store = await bootStore();
    const { notifyUserSwitch } = await import("../stores/userSwitch");
    await store.getState().enterChannel("chat");

    // authStore 换人时清指针 → 这里的 mock 同步退回 null
    persist.readLastSessionId.mockImplementation(() => null);
    notifyUserSwitch();
    await store.getState().enterChannel("chat");

    expect(store.getState().messages).toHaveLength(0);
    expect(historyApi.loadSessionMessages).toHaveBeenCalledTimes(1); // 只有换人前那一次
  });
});

describe("刷新恢复：指针的写入与清理", () => {
  it("发送路径把当前会话写成恢复指针（原缺陷：发送从不上报）", async () => {
    const store = await bootStore();
    store.setState({ datasourceId: 7, sessionId: "chat-live" });

    const pending = store.getState().sendMessage("各采购组织的收货数量");

    // 写在第一个 await 之前：指针必须在请求发出前就落地，否则「发出去了但刷新丢」
    expect(persist.writeLastSessionId).toHaveBeenCalledWith("chat", "chat-live");
    await pending;
  });

  it("写进去的指针在重启后真的能回放（写 → 重启 → 回放闭环）", async () => {
    // 模拟「上一次会话已把指针写进 localStorage」
    persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "chat-live" : null));
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "chat-live",
      messages: [readMsg({ id: 1, role: "user", content: "上一轮问题" })],
    });

    const store = await bootStore();
    await store.getState().enterChannel("chat");

    expect(store.getState().sessionId).toBe("chat-live");
    expect(store.getState().messages[0].content).toBe("上一轮问题");
  });

  it("「新对话」清掉指针（没有可恢复的目标，不该在刷新后恢复出一个空会话）", async () => {
    const store = await bootStore();

    store.getState().resetSession();

    expect(persist.writeLastSessionId).toHaveBeenCalledWith("chat", null);
    expect(store.getState().messages).toHaveLength(0);
  });

  it("删除当前会话时清指针；删别的会话不动指针", async () => {
    historyApi.deleteSessionHistory.mockResolvedValue(undefined);
    const store = await bootStore();

    await store.getState().deleteSession("other");
    expect(persist.writeLastSessionId).not.toHaveBeenCalledWith("chat", null);

    const current = store.getState().sessionId;
    await store.getState().deleteSession(current);
    expect(persist.writeLastSessionId).toHaveBeenCalledWith("chat", null);
    expect(store.getState().sessionId).not.toBe(current);
  });
});
