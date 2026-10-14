// Chat UI 状态 localStorage 持久化（手写 helper，不引入 zustand/middleware/persist）
//
// 持久化三类键，语义各不相同：
//
//   1. `qa:chat:lastSessionId:<channel>` —— **本渠道下次刷新要恢复哪个会话**的指针。
//      写入时机：发送路径（用户真的在这个会话里说话了）+ 点选历史项。
//      清空时机：登出/登录（authStore）、「新对话」、删除当前会话、回放失败。
//      **按渠道分键**：切到文档问答再回来，chat 的恢复目标不会被冲掉。
//   2. `qa:chat:lastChannel` —— 上次声明的渠道偏好（面板挂载时据此决定恢复面）。
//   3. `qa:chat:historyPanelOpen` —— 历史面板展开状态。
//
// 体量小，手写 try/catch 比引入 zustand/middleware/persist 依赖锁更稳。
//
// 为什么不做成 patch 式 `write({ lastSessionId })`：键名依赖 channel，patch 签名会退化成
// 调用方各自拼 key，漏一处就静默失效（历史上这个键恒为 null 就是这么来的）。

const PREFIX_LAST_SESSION_ID = "qa:chat:lastSessionId:";
const KEY_LAST_CHANNEL = "qa:chat:lastChannel";
const KEY_HISTORY_PANEL_OPEN = "qa:chat:historyPanelOpen";

/** 已知渠道（与 chatStore 的 channel 联合类型同源，改这里就够） */
export const CHAT_CHANNELS = ["chat", "doc_qa"] as const;

export type ChatChannel = (typeof CHAT_CHANNELS)[number];

function lastSessionIdKey(channel: ChatChannel): string {
  return `${PREFIX_LAST_SESSION_ID}${channel}`;
}

// localStorage 不可用（SSR / 隐私模式 / jsdom 尚未挂载）时返回 null，调用方走默认值分支。
// 探测本身也要 try：某些环境下读 window.localStorage 这个属性就会抛。
function safeStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}

export function readLastSessionId(channel: ChatChannel): string | null {
  const ls = safeStorage();
  if (!ls) return null;
  try {
    const raw = ls.getItem(lastSessionIdKey(channel));
    return raw && raw.length > 0 ? raw : null;
  } catch {
    return null;
  }
}

/** 写 null 等于清掉该渠道的恢复指针（「新对话」/ 回放失败用）。 */
export function writeLastSessionId(channel: ChatChannel, sessionId: string | null): void {
  const ls = safeStorage();
  if (!ls) return;
  try {
    if (sessionId === null) {
      ls.removeItem(lastSessionIdKey(channel));
    } else {
      ls.setItem(lastSessionIdKey(channel), sessionId);
    }
  } catch {
    // 隐私模式 / 配额超限：静默吞错，不阻塞 UI
  }
}

export function readLastChannel(): ChatChannel | null {
  const ls = safeStorage();
  if (!ls) return null;
  try {
    const raw = ls.getItem(KEY_LAST_CHANNEL);
    // 未知值（旧版本写入 / 手工改过）一律回退 null，别让垃圾值流进 store 的联合类型
    return CHAT_CHANNELS.find((c) => c === raw) ?? null;
  } catch {
    return null;
  }
}

export function writeLastChannel(channel: ChatChannel): void {
  const ls = safeStorage();
  if (!ls) return;
  try {
    ls.setItem(KEY_LAST_CHANNEL, channel);
  } catch {
    // 同上：持久化失败不影响本次会话
  }
}

export function readHistoryPanelOpen(): boolean {
  const ls = safeStorage();
  if (!ls) return false;
  try {
    return ls.getItem(KEY_HISTORY_PANEL_OPEN) === "true";
  } catch {
    return false;
  }
}

export function writeHistoryPanelOpen(open: boolean): void {
  const ls = safeStorage();
  if (!ls) return;
  try {
    ls.setItem(KEY_HISTORY_PANEL_OPEN, open ? "true" : "false");
  } catch {
    // 同上
  }
}

/**
 * 清掉**所有**渠道的恢复指针（登出 / 登录时调用）。
 *
 * 按前缀扫描而不是遍历 `CHAT_CHANNELS`：渠道增加时硬编码列表会静默漏清，
 * 漏掉的那个渠道在换用户后仍能被恢复 —— 那是跨用户串会话。面板状态与渠道偏好
 * 不属于「会话指针」，不在这里清。
 */
export function clearLastSessionIds(): void {
  const ls = safeStorage();
  if (!ls) return;
  try {
    const doomed: string[] = [];
    for (let i = 0; i < ls.length; i += 1) {
      const key = ls.key(i);
      if (key !== null && key.startsWith(PREFIX_LAST_SESSION_ID)) doomed.push(key);
    }
    for (const key of doomed) ls.removeItem(key);
  } catch {
    // 遍历/删除失败（隐私模式）时保持现状：宁可留指针，也不要半途抛错打断登出
  }
}
