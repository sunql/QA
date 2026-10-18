import { describe, it, expect, vi, beforeEach } from "vitest";
import { act } from "@testing-library/react";
import type { DocQaSseEvent } from "../types/document";

const docApi = vi.hoisted(() => ({ searchDocumentsQa: vi.fn() }));
vi.mock("../api/document", () => docApi);

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

const lastMessage = () => {
  const { messages } = useChatStore.getState();
  return messages[messages.length - 1];
};

describe("doc-QA 流的轮次所有权", () => {
  beforeEach(() => {
    useChatStore.setState({
      messages: [],
      sessionId: "docqa-test",
      channel: "doc_qa",
      loading: false,
      datasourceId: 1,
      error: null,
      currentAssistantMessageId: null,
      interrupted: false,
      sessions: [],
    });
    vi.clearAllMocks();
    historyApi.listChatSessions.mockResolvedValue([]);
  });

  it("doc-QA 流在途时切渠道并开始新一轮：旧流晚到帧不得写进新气泡或抢 loading", async () => {
    let emit!: (event: DocQaSseEvent) => void;
    let settleDocQa!: () => void;
    docApi.searchDocumentsQa.mockImplementation(
      (_payload: unknown, onEvent: (event: DocQaSseEvent) => void) =>
        new Promise<void>((resolve) => {
          emit = onEvent;
          settleDocQa = resolve;
        })
    );

    let docQaSend!: Promise<void>;
    await act(async () => {
      docQaSend = useChatStore.getState().sendDocQa("doc question", {});
      await Promise.resolve();
    });
    expect(useChatStore.getState().loading).toBe(true);
    expect(useChatStore.getState().messages).toHaveLength(2);

    // 切到 chat：doc-QA 流仍在跑（fetch 未 abort），但它的占位已被清出屏幕
    await act(async () => {
      await useChatStore.getState().enterChannel("chat");
    });
    expect(useChatStore.getState().loading).toBe(false);
    expect(useChatStore.getState().messages).toHaveLength(0);

    // 新一轮 chat 发送
    let chatHandlers!: {
      onToken?: (content: string) => void;
      onDone?: (p: typeof DONE_PAYLOAD) => void;
    };
    let settleChat!: () => void;
    chatApi.sendMessageStream.mockImplementation(
      (_payload: unknown, handlers: typeof chatHandlers) =>
        new Promise<void>((resolve) => {
          chatHandlers = handlers;
          settleChat = resolve;
        })
    );

    let chatSend!: Promise<void>;
    await act(async () => {
      chatSend = useChatStore.getState().sendMessage("chat question", true);
      await Promise.resolve();
    });
    const chatPointer = useChatStore.getState().currentAssistantMessageId;
    expect(chatPointer).not.toBeNull();
    expect(useChatStore.getState().loading).toBe(true);

    // 旧 doc-QA 流的 token / done 晚到：必须被所有权守卫丢弃
    await act(async () => {
      emit({ kind: "token", content: "DOCQA-LEAK" });
      emit({ kind: "done", tokensUsed: 1, cost: 0, modelName: null });
    });
    expect(lastMessage().content).toBe("");
    expect(lastMessage().role).toBe("assistant");
    expect(useChatStore.getState().loading).toBe(true);
    expect(useChatStore.getState().currentAssistantMessageId).toBe(chatPointer);

    // 旧流收尾（promise resolve）同样不得动新一轮的状态
    await act(async () => {
      settleDocQa();
      await docQaSend;
    });
    expect(useChatStore.getState().loading).toBe(true);
    expect(lastMessage().content).toBe("");

    // 新一轮正常收尾
    await act(async () => {
      chatHandlers.onDone?.(DONE_PAYLOAD);
      settleChat();
      await chatSend;
    });
    expect(useChatStore.getState().currentAssistantMessageId).toBeNull();
    expect(useChatStore.getState().loading).toBe(false);
  });

  it("doc-QA 流在途时切渠道：旧流失败不得把错误写进新渠道", async () => {
    let rejectDocQa!: (err: Error) => void;
    docApi.searchDocumentsQa.mockImplementation(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectDocQa = reject;
        })
    );

    let docQaSend!: Promise<void>;
    await act(async () => {
      docQaSend = useChatStore.getState().sendDocQa("doc question", {});
      await Promise.resolve();
    });

    await act(async () => {
      await useChatStore.getState().enterChannel("chat");
    });

    await act(async () => {
      rejectDocQa(new Error("doc-qa boom"));
      await docQaSend;
    });

    const state = useChatStore.getState();
    expect(state.messages).toHaveLength(0);
    expect(state.error).toBeNull();
    expect(state.loading).toBe(false);
  });
});
