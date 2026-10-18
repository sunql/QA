import { describe, it, expect, vi, beforeEach } from "vitest";
import { act } from "@testing-library/react";

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
  readLastSessionId: vi.fn<() => string | null>(() => null),
  writeLastSessionId: vi.fn<() => void>(),
  readLastChannel: vi.fn<() => string | null>(() => null),
  writeLastChannel: vi.fn<() => void>(),
  readHistoryPanelOpen: vi.fn<() => boolean>(() => false),
  writeHistoryPanelOpen: vi.fn<() => void>(),
}));
vi.mock("../stores/persistChatUiState", () => persist);

import { useChatStore } from "../stores/chatStore";

const DONE_PAYLOAD = {
  tokensUsed: 0,
  cost: 0,
  modelName: null,
  affinityStatus: null,
  agentRun: null,
  supplier360: null,
  supplierRisk: null,
  graphTraversal: null,
  suggestedAgent: null,
  visualRationale: null,
};

const runDone = (handlers: { onDone?: (p: typeof DONE_PAYLOAD) => void }) => {
  handlers.onDone?.(DONE_PAYLOAD);
  return Promise.resolve();
};

/** 模拟「上一轮流还在进行」：指针与占位消息都在。 */
const seedInFlight = () => {
  useChatStore.setState({
    messages: [
      {
        id: "assistant-1",
        role: "assistant",
        content: "",
        timestamp: 1,
        isStreaming: true,
        assistantMessageId: "stale-id",
      },
    ],
    sessionId: "s-test",
    loading: true,
    currentAssistantMessageId: "stale-id",
    interrupted: false,
  });
  // 正向断言种子生效：否则状态形状漂移时后面 7 个「清空」用例会静默空转
  expect(useChatStore.getState().currentAssistantMessageId).toBe("stale-id");
  expect(useChatStore.getState().loading).toBe(true);
};

describe("chatStore 流式绑定 assistantMessageId", () => {
  beforeEach(() => {
    useChatStore.setState({
      messages: [],
      sessionId: "s-test",
      loading: false,
      datasourceId: 1,
      error: null,
      currentAssistantMessageId: null,
      interrupted: false,
    });
    vi.clearAllMocks();
  });

  it("sendMessage 在 placeholder 时立即分配 assistantMessageId", async () => {
    chatApi.sendMessageStream.mockImplementation((_, handlers) => runDone(handlers));
    await act(async () => {
      await useChatStore.getState().sendMessage("test", true);
    });
    const assistantMsg = useChatStore.getState().messages.find((m) => m.role === "assistant");
    expect(assistantMsg?.assistantMessageId).toBeDefined();
    expect(assistantMsg?.aborted ?? false).not.toBe(true);
  });

  // 注意：断言必须放在 mock 之外，不能用「mock 里的 expect」。handler 在同一调用栈里
  // 执行，AssertionError 会被 sendMessage 的 catch 吞掉，测试仍然通过（假绿）。
  const pointerSnapshot = () => {
    const seen: Array<string | null | "unset"> = ["unset"];
    return {
      record: () => {
        seen[0] = useChatStore.getState().currentAssistantMessageId;
      },
      get: () => seen[0],
    };
  };

  it("onDone 在 handler 内立即清除 currentAssistantMessageId（不等 finally）", async () => {
    const snapshot = pointerSnapshot();
    chatApi.sendMessageStream.mockImplementation((_, handlers) => {
      handlers.onDone?.(DONE_PAYLOAD);
      snapshot.record();
      return Promise.resolve();
    });
    await act(async () => {
      await useChatStore.getState().sendMessage("test", true);
    });
    expect(snapshot.get()).toBeNull();
  });

  it("onError 在 handler 内立即清除 currentAssistantMessageId（不等 finally）", async () => {
    const snapshot = pointerSnapshot();
    chatApi.sendMessageStream.mockImplementation((_, handlers) => {
      handlers.onError?.("boom");
      snapshot.record();
      return Promise.resolve();
    });
    await act(async () => {
      await useChatStore.getState().sendMessage("test", true);
    });
    expect(snapshot.get()).toBeNull();
  });

  it("流异常（无 done/error）也在 finally 清除", async () => {
    chatApi.sendMessageStream.mockRejectedValue(
      new Error("stream interrupted: no done/error before EOF")
    );
    await act(async () => {
      try {
        await useChatStore.getState().sendMessage("test", true);
      } catch {
        /* expected: EOF 守门向上抛 */
      }
    });
    expect(useChatStore.getState().currentAssistantMessageId).toBeNull();
  });

  it("流进行中切数据源：孤儿流收尾后 loading 复位，输入框不卡死", async () => {
    let settle!: () => void;
    chatApi.sendMessageStream.mockImplementation(
      () => new Promise<void>((resolve) => {
        settle = resolve;
      })
    );

    let send!: Promise<void>;
    await act(async () => {
      send = useChatStore.getState().sendMessage("q", true);
      await Promise.resolve();
    });
    expect(useChatStore.getState().loading).toBe(true);

    await act(async () => {
      useChatStore.getState().setDatasourceId(2);
    });
    // 切数据源即作废在途轮次：指针清了，loading 必须同步复位
    expect(useChatStore.getState().currentAssistantMessageId).toBeNull();
    expect(useChatStore.getState().loading).toBe(false);

    // 孤儿流晚到的收尾不得把 loading 重新卡住
    await act(async () => {
      settle();
      await send;
    });
    expect(useChatStore.getState().loading).toBe(false);
    expect(useChatStore.getState().currentAssistantMessageId).toBeNull();
  });

  it("已完成的上一轮不会被下一次发送误标 aborted", async () => {
    chatApi.sendMessageStream.mockImplementation((_, handlers) => runDone(handlers));
    await act(async () => {
      await useChatStore.getState().sendMessage("first", true);
    });
    const firstId = useChatStore
      .getState()
      .messages.find((m) => m.role === "assistant")!.assistantMessageId;

    await act(async () => {
      await useChatStore.getState().sendMessage("second", true);
    });

    const firstAfter = useChatStore
      .getState()
      .messages.find((m) => m.assistantMessageId === firstId);
    // 已完成的一轮不是「被重发中止」，不能伪造 aborted 徽章
    expect(firstAfter?.aborted ?? false).toBe(false);
    const assistants = useChatStore.getState().messages.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(2);
    expect(assistants[assistants.length - 1].assistantMessageId).not.toBe(firstId);
  });

  it("本轮结束（onDone）后晚到的 token 不会改写消息", async () => {
    let handlers!: { onToken?: (content: string) => void };
    chatApi.sendMessageStream.mockImplementation((_, h) => {
      handlers = h;
      return runDone(h);
    });
    await act(async () => {
      await useChatStore.getState().sendMessage("test", true);
    });
    const assistant = () =>
      useChatStore.getState().messages.find((m) => m.role === "assistant")!;
    const afterDone = assistant().content;

    await act(async () => {
      handlers.onToken?.("late-frame");
    });

    // 指针已释放 ⇒ 严格守卫丢弃晚到帧（旧实现 null 放行，会把内容追加进来）
    expect(assistant().content).toBe(afterDone);
  });

  it("旧轮迟到失败不得清掉新一轮的指针、loading 或改写其消息", async () => {
    const streams: Array<{
      handlers: {
        onToken?: (content: string) => void;
        onDone?: (p: typeof DONE_PAYLOAD) => void;
      };
      settle: (err?: Error) => void;
    }> = [];
    chatApi.sendMessageStream.mockImplementation(
      (_, handlers) =>
        new Promise<void>((resolve, reject) => {
          streams.push({
            handlers,
            settle: (err) => (err ? reject(err) : resolve()),
          });
        })
    );

    let firstSend!: Promise<void>;
    await act(async () => {
      firstSend = useChatStore.getState().sendMessage("first", true);
      await Promise.resolve();
    });
    expect(streams).toHaveLength(1);
    const firstId = useChatStore.getState().currentAssistantMessageId;

    // 切会话：指针释放、loading=false，但底层 fetch 仍在跑
    await act(async () => {
      useChatStore.getState().resetSession();
    });

    let secondSend!: Promise<void>;
    await act(async () => {
      secondSend = useChatStore.getState().sendMessage("second", true);
      await Promise.resolve();
    });
    const secondId = useChatStore.getState().currentAssistantMessageId;
    expect(secondId).not.toBeNull();
    expect(secondId).not.toBe(firstId);

    // 旧轮迟到失败：不得抢走新一轮的指针
    await act(async () => {
      streams[0].settle(new Error("boom"));
      await firstSend.catch(() => {});
    });
    expect(useChatStore.getState().currentAssistantMessageId).toBe(secondId);
    expect(useChatStore.getState().loading).toBe(true);

    // 新一轮的帧仍然生效，且旧轮错误没有写进新一轮的气泡
    await act(async () => {
      streams[1].handlers.onToken?.("from-b");
    });
    const latest = () =>
      useChatStore.getState().messages[useChatStore.getState().messages.length - 1];
    expect(latest().content).toBe("from-b");
    expect(latest().isError ?? false).toBe(false);

    await act(async () => {
      streams[1].handlers.onDone?.(DONE_PAYLOAD);
      streams[1].settle();
      await secondSend;
    });
    expect(useChatStore.getState().currentAssistantMessageId).toBeNull();
  });

  it("上一轮流进行中：再次发送标 aborted，且不新建占位（loading 早返）", async () => {
    let resolveStream!: () => void;
    chatApi.sendMessageStream.mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveStream = resolve;
      })
    );

    let firstSend!: Promise<void>;
    await act(async () => {
      firstSend = useChatStore.getState().sendMessage("first", true);
      await Promise.resolve();
    });
    expect(useChatStore.getState().currentAssistantMessageId).not.toBeNull();

    await act(async () => {
      await useChatStore.getState().sendMessage("second", true);
    });

    const state = useChatStore.getState();
    const assistants = state.messages.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect(assistants[0].aborted).toBe(true);
    expect(assistants[0].isStreaming).toBe(false);
    // 指针仍指向被中止的那一轮，等它的流收尾时再清
    expect(state.currentAssistantMessageId).toBe(assistants[0].assistantMessageId);

    await act(async () => {
      resolveStream();
      await firstSend;
    });
    expect(useChatStore.getState().currentAssistantMessageId).toBeNull();
  });

  it("sendMessageStream 抛 'interrupted' → store.interrupted=true, message.interrupted=true", async () => {
    chatApi.sendMessageStream.mockRejectedValue(
      new Error("stream interrupted: no done/error before EOF")
    );
    await act(async () => {
      try {
        await useChatStore.getState().sendMessage("test", true);
      } catch {
        /* expected */
      }
    });
    const state = useChatStore.getState();
    expect(state.interrupted).toBe(true);
    expect(state.loading).toBe(false);
    const assistantMsg = state.messages.find((m) => m.role === "assistant");
    expect(assistantMsg?.interrupted).toBe(true);
    expect(assistantMsg?.isStreaming).toBe(false);
  });
});

describe("会话切换/清空必须释放 currentAssistantMessageId", () => {
  beforeEach(() => {
    useChatStore.setState({
      messages: [],
      sessionId: "s-test",
      loading: false,
      datasourceId: 1,
      error: null,
      currentAssistantMessageId: null,
      interrupted: false,
      sessions: [],
    });
    vi.clearAllMocks();
  });

  const expectCleared = async (action: () => void | Promise<unknown>) => {
    seedInFlight();
    await act(async () => {
      await action();
    });
    expect(useChatStore.getState().currentAssistantMessageId).toBeNull();
    // 指针被清 = 没有轮次拥有 loading；不回 false 会让孤儿流收尾后输入框永久禁用
    expect(useChatStore.getState().loading).toBe(false);
  };

  it("clearMessages", async () => {
    await expectCleared(() => useChatStore.getState().clearMessages());
  });

  it("resetSession", async () => {
    await expectCleared(() => useChatStore.getState().resetSession());
  });

  it("clearForUserSwitch", async () => {
    await expectCleared(() => useChatStore.getState().clearForUserSwitch());
  });

  it("setDatasourceId（已有对话 → 新会话）", async () => {
    await expectCleared(() => useChatStore.getState().setDatasourceId(2));
  });

  it("enterChannel（换渠道）", async () => {
    await expectCleared(() => useChatStore.getState().enterChannel("doc_qa"));
  });

  it("loadSessionMessages（载入另一条历史）", async () => {
    historyApi.loadSessionMessages.mockResolvedValue({ messages: [] });
    await expectCleared(() => useChatStore.getState().loadSessionMessages("s-other"));
  });

  it("deleteSession（删当前会话）", async () => {
    historyApi.deleteSessionHistory.mockResolvedValue(undefined);
    await expectCleared(() => useChatStore.getState().deleteSession("s-test"));
  });

  it("loadSessionMessages 失败（会话载不动）也释放指针与 loading", async () => {
    historyApi.loadSessionMessages.mockRejectedValue(new Error("404"));
    await expectCleared(() => useChatStore.getState().loadSessionMessages("s-gone"));
  });

  it("loadSessionMessages 失败后，被放弃的占位气泡不再永远转圈", async () => {
    historyApi.loadSessionMessages.mockRejectedValue(new Error("404"));
    await expectCleared(() => useChatStore.getState().loadSessionMessages("s-gone"));
    // 这一分支保留 messages（与 clearMessages 不同），孤儿占位必须就地收尾：
    // 否则它是一个永远 isStreaming、再也不会被任何帧更新的气泡。
    const abandoned = useChatStore.getState().messages.find((m) => m.id === "assistant-1");
    expect(abandoned?.isStreaming).toBe(false);
    expect(abandoned?.aborted).toBe(true);
  });
});
