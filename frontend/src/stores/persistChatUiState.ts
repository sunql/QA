// Chat UI 状态 localStorage 持久化（手写 helper，不引入 zustand/middleware/persist）
//
// 项目仅持久化 2 个键：当前会话 ID（用于刷新恢复）与历史面板展开状态。
// 体量小，手写 try/catch 比引入 zustand/middleware/persist 依赖锁更稳。

import type { ChatSession } from "../types/chatHistory";

const KEY_LAST_SESSION_ID = "qa:chat:lastSessionId";
const KEY_HISTORY_PANEL_OPEN = "qa:chat:historyPanelOpen";

export interface PersistedChatUiState {
  lastSessionId: ChatSession["sessionId"] | null;
  historyPanelOpen: boolean;
}

// 安全读取：localStorage 不可用（SSR/隐私模式）或 JSON 非法时回退到默认值
export function read(): PersistedChatUiState {
  const fallback: PersistedChatUiState = { lastSessionId: null, historyPanelOpen: false };
  if (typeof window === "undefined") return fallback;
  try {
    const rawLastId = window.localStorage.getItem(KEY_LAST_SESSION_ID);
    const rawOpen = window.localStorage.getItem(KEY_HISTORY_PANEL_OPEN);
    return {
      lastSessionId: rawLastId && rawLastId.length > 0 ? rawLastId : null,
      historyPanelOpen: rawOpen === "true",
    };
  } catch {
    return fallback;
  }
}

// 安全写入：仅 patch 中显式提供的字段被持久化；隐私/无 window 环境下静默 noop
export function write(patch: Partial<PersistedChatUiState>): void {
  if (typeof window === "undefined") return;
  try {
    if (patch.lastSessionId !== undefined) {
      if (patch.lastSessionId === null) {
        window.localStorage.removeItem(KEY_LAST_SESSION_ID);
      } else {
        window.localStorage.setItem(KEY_LAST_SESSION_ID, patch.lastSessionId);
      }
    }
    if (patch.historyPanelOpen !== undefined) {
      window.localStorage.setItem(
        KEY_HISTORY_PANEL_OPEN,
        patch.historyPanelOpen ? "true" : "false",
      );
    }
  } catch {
    // 隐私模式 / 配额超限：静默吞错，不阻塞 UI
  }
}