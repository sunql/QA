import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";

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

/** baseState：每个用例的起点，显式覆盖所有本文件断言涉及的字段。 */
const baseState = {
  messages: [] as any[],
  sessionId: "s-test",
  loading: false,
  datasourceId: 1,
  error: null,
  currentAssistantMessageId: null as string | null,
  interrupted: false,
  sessions: [] as any[],
  sessionsError: null,
};

describe("chatStore datasource scope", () => {
  beforeEach(() => {
    useChatStore.setState({ ...baseState } as any);
    vi.clearAllMocks();
  });

  it("setDatasourceId 切数据源时 reset messages / sessionId", () => {
    const { result } = renderHook(() => useChatStore());

    act(() => {
      result.current.addMessage({ id: "m1", role: "user", content: "hi", timestamp: Date.now() } as any);
    });
    const oldSid = result.current.sessionId;

    act(() => {
      result.current.setDatasourceId(2);
    });

    expect(result.current.datasourceId).toBe(2);
    expect(result.current.messages).toEqual([]);
    expect(result.current.sessionId).not.toBe(oldSid);
  });

  // 切源要把 loading / error / interrupted / currentAssistantMessageId 一并清掉，
  // 新数据源才能立刻提问，旧数据源的错误横幅也不会跟着切过来。
  //
  // 代价与配套：清掉之后在途流的收尾写入会打到新会话的消息上，所以必须同时有
  // streamGen 代次锁 + chatStore.datasourceSwitchStream.test.ts 的三条守卫。
  // 这里的断言只管「新源可用」，不管「新源不被旧流污染」。
  it("setDatasourceId 清 loading / error / interrupted，使新数据源可立即提问", () => {
    useChatStore.setState({
      loading: true,
      error: "旧数据源查询失败",
      interrupted: true,
      currentAssistantMessageId: "a-old",
    } as any);

    act(() => {
      useChatStore.getState().setDatasourceId(2);
    });

    const s = useChatStore.getState();
    expect(s.loading).toBe(false);
    expect(s.error).toBeNull();
    expect(s.interrupted).toBe(false);
    expect(s.currentAssistantMessageId).toBeNull();
  });

  // chatStore.ts:96 的不变量：凡「要重新决定屏幕上显示哪个会话」的动作都调
  // invalidatePendingLoads()。切数据源正是这种动作，但它曾是唯一漏掉的一条。
  // 漏掉 ⇒ loadSessionMessages 的 `seq !== sessionLoadSeq` 过期检查仍然放行，
  // 旧数据源的会话消息在切换之后落进新数据源的 store。
  it("切数据源时丢弃在途的历史加载（旧会话消息不得落到新数据源）", async () => {
    let resolveLoad: (v: any) => void = () => {};
    historyApi.loadSessionMessages.mockImplementation(
      () => new Promise((resolve) => { resolveLoad = resolve; })
    );

    // 起一个在途的历史加载（不 await）
    let pending: Promise<unknown>;
    await act(async () => {
      pending = useChatStore.getState().loadSessionMessages("s-old-ds", undefined);
      await Promise.resolve();
    });

    // 加载在途时切数据源
    act(() => {
      useChatStore.getState().setDatasourceId(2);
    });

    // 旧响应此时才回来
    await act(async () => {
      resolveLoad({ messages: [{ role: "user", content: "旧数据源的问题" }] });
      await pending!;
    });

    expect(useChatStore.getState().messages).toEqual([]);
    expect(useChatStore.getState().sessionId).not.toBe("s-old-ds");
  });

  // 反向断言：历史列表只按 channel 过滤（api/chatHistory.ts:16），不按数据源分域，
  // 所以切数据源不得清它 —— 清了会毁掉用户还能回去的合法历史。
  it("切数据源不清 sessions（历史不按数据源分域）", () => {
    useChatStore.setState({ sessions: [{ id: "s1" }], sessionsError: "旧告警" } as any);

    act(() => {
      useChatStore.getState().setDatasourceId(2);
    });

    const s = useChatStore.getState();
    expect(s.sessions).toEqual([{ id: "s1" }]);
    // sessionsError 一并断言：与 sessions 同属「历史面板」状态，两者要么都留
    // 要么都清。只断言其一的话，另一侧被顺手清掉不会有任何用例报警。
    expect(s.sessionsError).toBe("旧告警");
  });
});
