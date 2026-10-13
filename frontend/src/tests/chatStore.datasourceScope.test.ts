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
});
