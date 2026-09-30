import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  CHAT_CHANNELS,
  readLastSessionId,
  writeLastSessionId,
  readLastChannel,
  writeLastChannel,
  readHistoryPanelOpen,
  writeHistoryPanelOpen,
  clearLastSessionIds,
} from "../stores/persistChatUiState";

// 这一层是「刷新恢复」的地基：键的形状（按渠道分）和清键的边界（只清指针）都在这里钉死。
// 之所以要给它们单独写测试：chatStore 把整个模块 mock 掉了，所以 store 的测试**碰不到**
// 真实的 localStorage 行为 —— 这正是「qa:chat:lastSessionId 从来没被写入」能长期隐身的原因。
describe("persistChatUiState（按渠道分键）", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("不同渠道的恢复指针互不干扰", () => {
    writeLastSessionId("chat", "chat-1");
    writeLastSessionId("doc_qa", "docqa-1");

    expect(readLastSessionId("chat")).toBe("chat-1");
    expect(readLastSessionId("doc_qa")).toBe("docqa-1");
  });

  it("没写过的渠道读到 null（而不是别人的 id）", () => {
    writeLastSessionId("doc_qa", "docqa-1");

    expect(readLastSessionId("chat")).toBeNull();
  });

  it("写 null 等价于删除该渠道的键", () => {
    writeLastSessionId("chat", "chat-1");

    writeLastSessionId("chat", null);

    expect(readLastSessionId("chat")).toBeNull();
    expect(window.localStorage.getItem("qa:chat:lastSessionId:chat")).toBeNull();
  });

  it("空字符串不算有效指针", () => {
    window.localStorage.setItem("qa:chat:lastSessionId:chat", "");

    expect(readLastSessionId("chat")).toBeNull();
  });

  it("clearLastSessionIds 清掉所有渠道的指针，但不误删其他键", () => {
    writeLastSessionId("chat", "chat-1");
    writeLastSessionId("doc_qa", "docqa-1");
    writeHistoryPanelOpen(true);
    writeLastChannel("doc_qa");

    clearLastSessionIds();

    expect(readLastSessionId("chat")).toBeNull();
    expect(readLastSessionId("doc_qa")).toBeNull();
    // 面板展开状态与渠道偏好不属于「会话指针」，登出不该顺手抹掉它们
    expect(readHistoryPanelOpen()).toBe(true);
    expect(readLastChannel()).toBe("doc_qa");
  });

  it("clearLastSessionIds 按前缀扫描，能清掉枚举之外的渠道", () => {
    // wiki chat（wikicha- 前缀）不在 CHAT_CHANNELS 里，但同前缀的键必须一起清 ——
    // 硬编码渠道列表会在渠道增加时静默漏清（登出后仍能被恢复）。
    window.localStorage.setItem("qa:chat:lastSessionId:wiki_qa", "wikicha-1");

    clearLastSessionIds();

    expect(window.localStorage.getItem("qa:chat:lastSessionId:wiki_qa")).toBeNull();
  });

  it("lastChannel 只认已知渠道，垃圾值回退 null", () => {
    writeLastChannel("chat");
    expect(readLastChannel()).toBe("chat");

    window.localStorage.setItem("qa:chat:lastChannel", "not-a-channel");
    expect(readLastChannel()).toBeNull();
  });

  it("historyPanelOpen 往返 true/false", () => {
    expect(readHistoryPanelOpen()).toBe(false);

    writeHistoryPanelOpen(true);
    expect(readHistoryPanelOpen()).toBe(true);

    writeHistoryPanelOpen(false);
    expect(readHistoryPanelOpen()).toBe(false);
  });

  // 这不是「验证类型一致」的断言（联合类型就是从这个数组派生的，改不了），
  // 而是一条**绊线**：渠道增删时逼人回来看一眼清键与分键是否还成立。
  it("渠道枚举是显式白名单（增删渠道时 revisits 清键/分键逻辑）", () => {
    expect([...CHAT_CHANNELS]).toEqual(["chat", "doc_qa"]);
  });

  it("localStorage 读写抛错时静默回退，不把异常抛给调用方", () => {
    // 隐私模式 / 配额超限：读要能拿到默认值，写不能炸掉发送路径
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });

    expect(readLastSessionId("chat")).toBeNull();
    expect(readLastChannel()).toBeNull();
    expect(readHistoryPanelOpen()).toBe(false);
    expect(() => writeLastSessionId("chat", "chat-1")).not.toThrow();
    expect(() => writeHistoryPanelOpen(true)).not.toThrow();
    expect(() => writeLastChannel("chat")).not.toThrow();
  });
});
