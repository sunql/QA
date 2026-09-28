// Wiki Chat 会话状态（feat-wiki-chat）。
//
// 独立 store，不复用全局 chatStore（chat 与 doc_qa 共用一个 store 导致消息
// 状态互相污染的教训）。会话历史走 /sessions/chat-history（channel=wiki_qa）。

import { create } from "zustand";
import { sendWikiChat } from "../api/wikiChat";
import {
  listChatSessions,
  loadSessionMessages as fetchSessionMessages,
  deleteSessionHistory as apiDeleteSession,
} from "../api/chatHistory";
import type { ChatSession } from "../types/chatHistory";
import type { WikiChatMessage } from "../types/wikiChat";

// 流式占位：以新增对象替换最后一条消息（不可变更新）
function patchLastMessage(
  messages: WikiChatMessage[],
  patch: Partial<WikiChatMessage>,
): WikiChatMessage[] {
  const last = messages[messages.length - 1];
  if (!last) return messages;
  return [...messages.slice(0, -1), { ...last, ...patch }];
}

interface WikiChatState {
  messages: WikiChatMessage[];
  sessionId: string;
  loading: boolean;
  error: string | null;
  // 历史会话面板
  sessions: ChatSession[];
  sessionsLoading: boolean;
  // 模型选择（null = 自动路由，照 ChatPage 的 selectedModelId 语义）
  selectedModelId: number | null;

  send: (question: string, opts?: { dimension?: string }) => Promise<void>;
  setSelectedModelId: (id: number | null) => void;
  loadSessions: () => Promise<void>;
  loadSessionMessages: (sessionId: string) => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;
  resetSession: () => void;
}

function newSessionId(): string {
  return `wikicha-${crypto.randomUUID()}`;
}

export const useWikiChatStore = create<WikiChatState>()((set, get) => ({
  messages: [],
  sessionId: newSessionId(),
  loading: false,
  error: null,
  sessions: [],
  sessionsLoading: false,
  selectedModelId: null,

  send: async (question, opts) => {
    const { sessionId, selectedModelId } = get();
    const userMsg: WikiChatMessage = { role: "user", content: question };
    const placeholder: WikiChatMessage = { role: "assistant", content: "" };
    set((state) => ({
      messages: [...state.messages, userMsg, placeholder],
      loading: true,
      error: null,
    }));

    try {
      await sendWikiChat(
        {
          sessionId,
          question,
          topK: 8,
          dimension: opts?.dimension,
          modelId: selectedModelId ?? undefined,
        },
        (event) => {
          switch (event.kind) {
            case "citations":
              set((state) => ({
                messages: patchLastMessage(state.messages, { citations: event.citations ?? [] }),
              }));
              break;
            case "token":
              set((state) => {
                const last = state.messages[state.messages.length - 1];
                return {
                  messages: patchLastMessage(state.messages, {
                    content: (last?.content ?? "") + (event.content ?? ""),
                  }),
                };
              });
              break;
            case "error":
              set((state) => ({
                messages: patchLastMessage(state.messages, {
                  content: (state.messages[state.messages.length - 1]?.content ?? "") +
                    (event.error ?? ""),
                }),
                error: event.error ?? "error",
              }));
              break;
            case "meta":
            case "done":
            default:
              break;
          }
        },
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      set((state) => ({
        messages: patchLastMessage(state.messages, {
          content: (state.messages[state.messages.length - 1]?.content ?? "") || msg,
        }),
        error: msg,
      }));
    } finally {
      set({ loading: false });
    }
  },

  setSelectedModelId: (id) => set({ selectedModelId: id }),

  loadSessions: async () => {
    set({ sessionsLoading: true });
    try {
      const sessions = await listChatSessions(50, 0, "wiki_qa");
      set({ sessions });
    } catch {
      set({ sessions: [] });
    } finally {
      set({ sessionsLoading: false });
    }
  },

  loadSessionMessages: async (sessionId) => {
    set({ loading: true });
    try {
      const res = await fetchSessionMessages(sessionId);
      set({
        sessionId,
        messages: res.messages.map((m) => ({
          role: m.role,
          content: m.content,
        })),
      });
    } catch {
      set({ messages: [] });
    } finally {
      set({ loading: false });
    }
  },

  deleteSession: async (sessionId) => {
    // 后端 DELETE /sessions/{id}：message + token_usage + query_state 三表同删
    await apiDeleteSession(sessionId);
    set((state) => {
      const nextSessions = state.sessions.filter((s) => s.sessionId !== sessionId);
      if (state.sessionId !== sessionId) {
        return { sessions: nextSessions };
      }
      // 删除的是当前会话：清空消息并换新 sessionId（照 chatStore.deleteSession 语义）
      return {
        sessions: nextSessions,
        sessionId: newSessionId(),
        messages: [],
        loading: false,
        error: null,
      };
    });
  },

  resetSession: () => {
    set({ messages: [], sessionId: newSessionId(), error: null });
  },
}));
