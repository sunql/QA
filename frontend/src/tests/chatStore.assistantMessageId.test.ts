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

describe("chatStore 流式绑定 assistantMessageId", () => {
  beforeEach(() => {
    useChatStore.setState({
      messages: [],
      sessionId: "s-test",
      loading: false,
      datasourceId: 1,
      error: null,
      currentRunId: null,
      currentAssistantMessageId: null,
      interrupted: false,
    } as any);
    vi.clearAllMocks();
  });

  it("sendMessage 在 placeholder 时立即分配 assistantMessageId", async () => {
    chatApi.sendMessageStream.mockImplementation((_, handlers) => {
      handlers.onDone?.({ tokensUsed: 0, cost: 0, modelName: null, affinityStatus: null, agentRun: null, supplier360: null, supplierRisk: null, graphTraversal: null, suggestedAgent: null, visualRationale: null });
      return Promise.resolve();
    });
    await act(async () => {
      await useChatStore.getState().sendMessage("test", true);
    });
    // currentAssistantMessageId 已被 onDone 清除；检查消息对象的 assistantMessageId
    const assistantMsg = useChatStore.getState().messages.find((m) => m.role === "assistant");
    expect(assistantMsg?.assistantMessageId).toBeDefined();
    expect(assistantMsg?.aborted ?? false).not.toBe(true); // 未被 abort
  });

  it("重发：旧 assistantMessageId 标 aborted；新 id 替换", async () => {
    chatApi.sendMessageStream.mockImplementation((_, handlers) => {
      handlers.onDone?.({ tokensUsed: 0, cost: 0, modelName: null, affinityStatus: null, agentRun: null, supplier360: null, supplierRisk: null, graphTraversal: null, suggestedAgent: null, visualRationale: null });
      return Promise.resolve();
    });
    // 第一次调用
    await act(async () => {
      await useChatStore.getState().sendMessage("first", true);
    });
    // 从消息对象捕获第一次的 assistantMessageId（此时 store.currentAssistantMessageId 已由 onDone 清除）
    const msgsAfterFirst = useChatStore.getState().messages;
    const firstAssistantMsg = msgsAfterFirst.find((m) => m.role === "assistant");
    expect(firstAssistantMsg?.assistantMessageId).toBeDefined();
    // aborted 正常完成时应为 undefined 或 false（未被重发取消）
    expect(firstAssistantMsg?.aborted ?? false).toBe(false);
    const firstId = firstAssistantMsg!.assistantMessageId;

    // 第二次调用
    await act(async () => {
      await useChatStore.getState().sendMessage("second", true);
    });

    // 第二次调用结束后，第一条助手消息应被标记 aborted
    const msgsAfterSecond = useChatStore.getState().messages;
    const firstAfter = msgsAfterSecond.find((m) => m.assistantMessageId === firstId);
    expect(firstAfter?.aborted).toBe(true);
    expect(firstAfter?.isStreaming).toBe(false);

    // 第二条助手消息有不同 id
    const secondAfter = msgsAfterSecond.filter((m) => m.role === "assistant");
    expect(secondAfter).toHaveLength(2);
    const latestAssistant = secondAfter[secondAfter.length - 1];
    expect(latestAssistant.assistantMessageId).not.toBe(firstId);
  });

  it("sendMessageStream 抛 'interrupted' → store.interrupted=true, message.interrupted=true", async () => {
    chatApi.sendMessageStream.mockRejectedValue(new Error("stream interrupted: no done/error before EOF"));
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
