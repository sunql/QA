import { act, renderHook } from "@testing-library/react";
import { useChatStore } from "../stores/chatStore";

describe("chatStore datasource scope", () => {
  it("setDatasourceId 切数据源时 reset messages / sessionId / isStreaming", () => {
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

  it("H1: setDatasourceId 清 currentAssistantMessageId / interrupted / error — 防 in-flight SSE 跨会话污染", () => {
    // P2-1 security review H1 落地：切数据源时如果留旧 assistantMessageId，
    // 旧 SSE 帧的 isCurrent() 守卫仍能通过（id 一致），会写进新会话的 messages
    const { result } = renderHook(() => useChatStore());

    act(() => {
      // 模拟流式进行中
      (result.current as any).currentAssistantMessageId = "old-msg-id";
      (result.current as any).interrupted = true;
      (result.current as any).error = "旧错误";
      result.current.setDatasourceId(1);
    });

    act(() => {
      result.current.setDatasourceId(2);
    });

    expect((result.current as any).currentAssistantMessageId).toBeNull();
    expect((result.current as any).interrupted).toBe(false);
    expect(result.current.error).toBeNull();
    expect((result.current as any).loading).toBe(false);
    expect((result.current as any).isStreaming).toBe(false);
  });

  it("H1: resetSession / clearForUserSwitch 同样清 currentAssistantMessageId", () => {
    // 新对话 / 换人两条路径都要清，否则旧 SSE 帧能进新会话
    const { result } = renderHook(() => useChatStore());

    act(() => {
      (result.current as any).currentAssistantMessageId = "old-msg-id";
      result.current.resetSession();
    });
    expect((result.current as any).currentAssistantMessageId).toBeNull();
    expect((result.current as any).interrupted).toBe(false);
    expect((result.current as any).isStreaming).toBe(false);

    act(() => {
      (result.current as any).currentAssistantMessageId = "old-msg-id-2";
      result.current.clearForUserSwitch();
    });
    expect((result.current as any).currentAssistantMessageId).toBeNull();
    expect((result.current as any).interrupted).toBe(false);
  });
});

