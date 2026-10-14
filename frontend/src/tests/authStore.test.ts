import { describe, it, expect, vi, beforeEach } from "vitest";
import { useAuthStore } from "../stores/authStore";
import { useChatStore } from "../stores/chatStore";
import { authApi } from "../api/auth";
import { readLastSessionId, writeLastSessionId } from "../stores/persistChatUiState";
import type { ChatMessage } from "../types/chat";

vi.mock("../api/auth");
vi.mock("../api/client", () => ({
  apiClient: { defaults: { headers: { common: {} } } },
}));

/** 上一个用户（alice）留在内存里的那条对话。 */
const aliceMessage: ChatMessage = {
  id: "m-alice-1",
  role: "user",
  content: "alice 上一场的提问",
  timestamp: Date.now(),
};

describe("authStore", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, user: null, mustChangePassword: false, rememberMe: false });
    useChatStore.setState({ messages: [], sessionId: "chat-boot", sessions: [] });
    window.localStorage.clear();
  });

  it("login 成功设置 token + mustChangePassword + 调 fetchMe", async () => {
    (authApi.login as any).mockResolvedValue({
      accessToken: "t1", tokenType: "Bearer", expiresIn: 3600,
      mustChangePassword: true, user: { id: 1, username: "admin", displayName: "Admin", email: null, roles: [], organizations: [] },
    });
    (authApi.getMe as any).mockResolvedValue({ id: 1, username: "admin" });
    await useAuthStore.getState().login("admin", "Admin@123", true);
    expect(useAuthStore.getState().token).toBe("t1");
    expect(useAuthStore.getState().mustChangePassword).toBe(true);
    expect(useAuthStore.getState().user).toEqual({ id: 1, username: "admin" });
  });

  it("logout 静默吞 401", async () => {
    useAuthStore.setState({ token: "t1" });
    (authApi.logout as any).mockRejectedValue(new Error("401"));
    await useAuthStore.getState().logout();
    expect(useAuthStore.getState().token).toBeNull();
  });

  it("changeOwnPassword 成功后清态", async () => {
    useAuthStore.setState({ token: "t1" });
    (authApi.changeOwnPassword as any).mockResolvedValue(undefined);
    (authApi.logout as any).mockRejectedValue(new Error("401"));
    await useAuthStore.getState().changeOwnPassword("OldPwd1", "NewPwd1");
    expect(useAuthStore.getState().token).toBeNull();
  });

  // 下面三条是「刷新恢复」的生命周期边界：会话指针只在**同一个登录会话内**有效。
  // 少了它们，换用户（或改密重登）后新用户会把上一个用户的会话恢复到自己屏幕上。
  it("logout 清掉所有渠道的会话恢复指针", async () => {
    writeLastSessionId("chat", "chat-alice");
    writeLastSessionId("doc_qa", "docqa-alice");
    (authApi.logout as any).mockResolvedValue(undefined);

    await useAuthStore.getState().logout();

    expect(readLastSessionId("chat")).toBeNull();
    expect(readLastSessionId("doc_qa")).toBeNull();
  });

  it("login 成功也清指针（不登出直接换用户这条路径）", async () => {
    writeLastSessionId("chat", "chat-alice");
    (authApi.login as any).mockResolvedValue({
      accessToken: "t2", tokenType: "Bearer", expiresIn: 3600,
      mustChangePassword: false, user: { id: 2, username: "bob" },
    });
    (authApi.getMe as any).mockResolvedValue({ id: 2, username: "bob" });

    await useAuthStore.getState().login("bob", "Bob@12345", true);

    expect(readLastSessionId("chat")).toBeNull();
  });

  it("login 失败（口令错）不清指针 —— 老用户还在，恢复目标不该被丢掉", async () => {
    writeLastSessionId("chat", "chat-alice");
    (authApi.login as any).mockRejectedValue(new Error("401"));

    await expect(useAuthStore.getState().login("bob", "wrong", true)).rejects.toThrow();

    expect(readLastSessionId("chat")).toBe("chat-alice");
  });

  // 下面两条是「换人」的**内存**一侧（指针那侧见上面三条）。共享终端上不刷新页面就换人时，
  // store 里上一个用户的 messages 会留在屏幕上 —— 自动恢复没有「点一下」这个人为闸门了。
  // 用真 chatStore 断言，顺带证明 chatStore 的换人监听真的注册上了。
  it("logout 清掉内存里的对话（下一个人看不到上一场对话）", async () => {
    useChatStore.setState({ messages: [aliceMessage], sessionId: "chat-alice" });
    (authApi.logout as any).mockResolvedValue(undefined);

    await useAuthStore.getState().logout();

    expect(useChatStore.getState().messages).toHaveLength(0);
    expect(useChatStore.getState().sessionId).not.toBe("chat-alice");
  });

  it("login 成功也清内存对话（不登出直接换用户这条路径）", async () => {
    useChatStore.setState({ messages: [aliceMessage], sessionId: "chat-alice" });
    (authApi.login as any).mockResolvedValue({
      accessToken: "t3", tokenType: "Bearer", expiresIn: 3600,
      mustChangePassword: false, user: { id: 2, username: "bob" },
    });
    (authApi.getMe as any).mockResolvedValue({ id: 2, username: "bob" });

    await useAuthStore.getState().login("bob", "Bob@12345", true);

    expect(useChatStore.getState().messages).toHaveLength(0);
    expect(useChatStore.getState().sessionId).not.toBe("chat-alice");
  });
});
