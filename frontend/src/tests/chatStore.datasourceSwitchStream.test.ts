import { describe, expect, it, vi, beforeEach } from "vitest";
import { act } from "@testing-library/react";

/**
 * 切数据源时「在途流」不得写进新会话。
 *
 * 与 `chatStore.datasourceScope.test.ts` 分开：那个文件守的是**在途历史加载**
 * （`loadSessionMessages`），这里守的是**在途 SSE 流**（`sendMessage`）。
 * 两者失效机理不同：前者靠 `sessionLoadSeq`，后者靠 `streamGen`。
 *
 * 为什么必须有这个守卫：`sendMessage` 里 12 个 SSE handler 有 `isCurrent()`
 * 兜底，但 await 之后的收尾写入（finally / catch / 假设回填 / 非流式响应）
 * **全部绕过 `isCurrent()`**，且用的是 `patchLastMessage` —— 它无条件写「最后
 * 一条」。切数据源后新流已经把消息追加进列表，于是旧流的收尾写入会精确命中
 * 新会话的消息。
 */

const chatApi = vi.hoisted(() => ({
  fetchHypotheses: vi.fn<() => Promise<any[]>>(async () => []),
  resumeMultiStepRun: vi.fn(async () => {}),
  sendMessage: vi.fn(async () => ({})),
  sendMessageStream: vi.fn(),
}));
vi.mock("../api/chat", () => chatApi);

const persist = vi.hoisted(() => ({
  readLastSessionId: vi.fn<() => string | null>(() => null),
  writeLastSessionId: vi.fn(),
  readLastChannel: vi.fn<() => string | null>(() => null),
  writeLastChannel: vi.fn(),
  readHistoryPanelOpen: vi.fn<() => boolean>(() => false),
  writeHistoryPanelOpen: vi.fn(),
}));
vi.mock("../stores/persistChatUiState", () => persist);

import { useChatStore } from "../stores/chatStore";

const baseState = {
  messages: [] as any[],
  sessionId: "s-test",
  loading: false,
  datasourceId: 1,
  error: null,
  currentAssistantMessageId: null as string | null,
  interrupted: false,
  selectedModelId: null,
  channel: "chat" as const,
};

describe("切数据源时的在途流", () => {
  beforeEach(() => {
    useChatStore.setState({ ...baseState } as any);
    vi.clearAllMocks();
    chatApi.fetchHypotheses.mockResolvedValue([]);
  });

  /**
   * 起一条在途流：mock 抓住 handlers，并返回一个由测试自己兑现的 promise。
   * 返回 settle，用于「让这条流结束（不发 done/error）」或「让这条流报错」。
   */
  function startStream() {
    let captured: any = null;
    let settle!: (err?: Error) => void;
    chatApi.sendMessageStream.mockImplementationOnce(async (_payload: unknown, handlers: any) => {
      captured = handlers;
      await new Promise<void>((resolve, reject) => {
        settle = (err) => (err ? reject(err) : resolve());
      });
    });
    return {
      get handlers() {
        return captured;
      },
      // 必须用 getter：settle 是在 mock 被调用时才赋值的，按值返回会永远拿到 undefined。
      get settle() {
        return settle;
      },
    };
  }

  const lastAssistant = () => {
    const msgs = useChatStore.getState().messages;
    return msgs[msgs.length - 1];
  };

  it("旧流收尾（finally）不得把新流的 loading 与 isStreaming 复位", async () => {
    const a = startStream();

    // ① 旧数据源开问（不 await —— 流在途）
    let pendingA!: Promise<void>;
    await act(async () => {
      pendingA = useChatStore.getState().sendMessage("旧数据源的问题", true);
      await Promise.resolve();
    });

    // ② 流在途时切数据源
    act(() => {
      useChatStore.getState().setDatasourceId(2);
    });

    // ③ 新数据源立刻开问（这一步证明 loading:false 确实让新源可用）
    const b = startStream();
    let pendingB!: Promise<void>;
    await act(async () => {
      pendingB = useChatStore.getState().sendMessage("新数据源的问题", true);
      await Promise.resolve();
    });
    expect(useChatStore.getState().loading).toBe(true);

    // ④ 旧流此刻才结束（既没 onDone 也没 onError ⇒ 走 finally 兜底分支）
    await act(async () => {
      a.settle();
      await pendingA;
    });

    // 旧流的 finally 若执行，会把新流的 loading 置回 false、
    // 并给**新流的消息**打上 isStreaming:false —— 新流明明还在跑。
    const s = useChatStore.getState();
    expect(s.loading, "旧流 finally 把新流的 loading 复位了").toBe(true);
    expect(lastAssistant()?.isStreaming, "旧流 finally 给新流消息打了 isStreaming:false").toBe(true);

    await act(async () => {
      b.settle();
      await pendingB;
    });
  });

  // 注：曾有一个「旧流假设回填落到新会话」的用例。它是**假绿**——已核实两个
  // attachHypotheses 调用点（:779 onDone 内、:844 非流式分支）都排在
  // `isCurrent()` 之后，id 检查早已挡住，改不改修复它都绿。已删除，改守下面
  // 这条真正没有守卫的路径。
  it("旧流的非流式响应不得覆写新会话消息、也不得清掉新流的 assistant id", async () => {
    let resolveA!: (v: any) => void;
    chatApi.sendMessage.mockImplementationOnce(
      () => new Promise((resolve) => { resolveA = resolve; })
    );

    let pendingA!: Promise<void>;
    await act(async () => {
      pendingA = useChatStore.getState().sendMessage("旧数据源的问题", false);
      await Promise.resolve();
    });

    act(() => {
      useChatStore.getState().setDatasourceId(2);
    });

    // 新流也保持在途：否则它会立刻走完并把自己的 id 清掉，断言就抓不到「旧流
    // 清掉新流 id」这个瞬间。
    let resolveB!: (v: any) => void;
    chatApi.sendMessage.mockImplementationOnce(
      () => new Promise((resolve) => { resolveB = resolve; })
    );
    let pendingB!: Promise<void>;
    await act(async () => {
      pendingB = useChatStore.getState().sendMessage("新数据源的问题", false);
      await Promise.resolve();
    });
    const newAssistantId = useChatStore.getState().currentAssistantMessageId;
    expect(newAssistantId).not.toBeNull();

    await act(async () => {
      resolveA({ answer: "旧数据源的回答" });
      await pendingA;
    });

    expect(lastAssistant()?.content, "旧流的非流式响应覆写了新会话消息的正文").not.toBe(
      "旧数据源的回答"
    );
    // 旧流顺手把 currentAssistantMessageId 清成 null —— 这会让仍在飞的任何流的
    // isCurrent() 因 `id === null` 直接放行，等于拆掉全部 12 个 handler 的守卫。
    expect(
      useChatStore.getState().currentAssistantMessageId,
      "旧流把新流的 assistant id 清成了 null"
    ).toBe(newAssistantId);

    await act(async () => {
      resolveB({ answer: "新数据源的回答" });
      await pendingB;
    });
  });

  it("旧流的报错不得覆写新会话消息的正文", async () => {
    const a = startStream();
    let pendingA!: Promise<void>;
    await act(async () => {
      pendingA = useChatStore.getState().sendMessage("旧数据源的问题", true).catch(() => {});
      await Promise.resolve();
    });

    act(() => {
      useChatStore.getState().setDatasourceId(2);
    });

    const b = startStream();
    let pendingB!: Promise<void>;
    await act(async () => {
      pendingB = useChatStore.getState().sendMessage("新数据源的问题", true);
      await Promise.resolve();
    });

    await act(async () => {
      a.settle(new Error("旧数据源炸了"));
      await pendingA;
    });

    const last = lastAssistant();
    expect(last?.content, "旧流的报错覆写了新会话消息的正文").not.toBe("旧数据源炸了");
    expect(last?.isError, "旧流的报错给新会话消息打了 isError").toBeFalsy();
    expect(useChatStore.getState().error, "旧流的报错挂到了全局错误横幅上").toBeNull();

    await act(async () => {
      b.settle();
      await pendingB;
    });
  });
});
