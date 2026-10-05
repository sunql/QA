// 研究型 Agent 入口会话状态（feat-research-entry Task 9）。
//
// 独立 store，不复用全局 chatStore（chat 与 doc_qa 共用一个 store 导致消息状态
// 互相污染的教训）。进度经独立 SSE 端点（research.* 事件族）流入，按事件名分发
// （事件名本身就是语义，不能像 wikiChat 那样压成 kind）。

import { create } from "zustand";
import {
  answerCheckpoint as apiAnswerCheckpoint,
  createResearchSession,
  deleteResearchSession as apiDeleteResearchSession,
  getReport as apiGetReport,
  getResearchSession,
  listReports as apiListReports,
  listResearchSessions,
  openResearchStream,
  submitTurn as apiSubmitTurn,
} from "../api/research";
import type {
  CheckpointAction,
  CheckpointPhase,
  ResearchCheckpoint,
  ResearchErrorPayload,
  ResearchMode,
  ResearchReport,
  ResearchReportSummary,
  ResearchSession,
  ResearchSseEvent,
  ResearchTurn,
} from "../types/research";

const CHECKPOINT_PHASES: ReadonlySet<string> = new Set<string>([
  "intent",
  "planning",
  "hypothesis",
  "runtime_dynamic",
  "low_confidence_step",
]);

function isCheckpointPhase(value: unknown): value is CheckpointPhase {
  return typeof value === "string" && CHECKPOINT_PHASES.has(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// 终态判定只按 uiHint 的「类」分支（用户裁定规则 ③）：绝不按 code 逐个判断，
// 后端新增终态 code 时前端零改动。payload 从网络 JSON 解析而来，运行时只认
// `=== "terminal"` 收窄；其余（degraded / 字段缺失）一律走降级分支。
type TerminalErrorPayload = ResearchErrorPayload & { uiHint: "terminal" };

function isTerminalError(payload: unknown): payload is TerminalErrorPayload {
  return isRecord(payload) && payload.uiHint === "terminal";
}

// SSE research.checkpoint payload → store pendingCheckpoint。SSE 只下发
// checkpointId/phase/prompt/options，REST 形状的 status/userChoice/decidedAt 补默认值。
function checkpointFromEvent(payload: Record<string, unknown>): ResearchCheckpoint {
  return {
    id: typeof payload.checkpointId === "string" ? payload.checkpointId : "",
    phase: isCheckpointPhase(payload.phase) ? payload.phase : "intent",
    status: "pending",
    options: isRecord(payload.options) ? payload.options : {},
    prompt: typeof payload.prompt === "string" ? payload.prompt : "",
    userChoice: null,
    decidedAt: null,
  };
}

// 单个 SSE 事件 → 状态增量（不可变：events 用展开运算符追加，绝不 push 原数组）。
function applyEvent(state: ResearchState, event: ResearchSseEvent): Partial<ResearchState> {
  const events = [...state.events, event];
  switch (event.name) {
    case "research.checkpoint":
      return { events, pendingCheckpoint: checkpointFromEvent(event.payload) };
    case "research.done":
      return { events, streaming: false };
    case "research.error": {
      // 处置只按 uiHint 的「类」分支，绝不按 code 逐个判断（用户裁定规则 ③）。
      if (isTerminalError(event.payload)) {
        return {
          events,
          streaming: false,
          error: typeof event.payload.message === "string" ? event.payload.message : "research.error",
        };
      }
      // 降级类（degraded / 字段缺失）：流保持打开，状态机会继续推进，后面可能还有 done。
      return { events };
    }
    default:
      return { events };
  }
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

interface ResearchState {
  sessions: ResearchSession[];
  sessionsLoading: boolean;
  currentSession: ResearchSession | null;
  turns: ResearchTurn[];
  events: ResearchSseEvent[];
  pendingCheckpoint: ResearchCheckpoint | null;
  report: ResearchReport | null;
  reports: ResearchReportSummary[];
  loading: boolean;
  streaming: boolean;
  error: string | null;

  loadSessions: () => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;
  openSession: (sessionId: string) => Promise<void>;
  sendQuestion: (question: string, mode?: ResearchMode) => Promise<string>;
  submitTurn: (sessionId: string, question: string) => Promise<void>;
  answer: (
    checkpointId: string,
    action: CheckpointAction,
    choice?: Record<string, unknown>,
  ) => Promise<void>;
  loadReport: (sessionId: string, version?: number) => Promise<void>;
  loadReports: (sessionId: string) => Promise<void>;
  connectStream: (
    sessionId: string,
    signal?: AbortSignal,
    onOpen?: () => void,
  ) => Promise<void>;
  reset: () => void;
}

// 流中断句柄：新流建立前先断旧流（组件卸载 / 切会话 / 重复 connectStream），
// 避免旧会话的事件串进新会话的 events。
let activeAbort: AbortController | null = null;

export const useResearchStore = create<ResearchState>()((set, get) => ({
  sessions: [],
  sessionsLoading: false,
  currentSession: null,
  turns: [],
  events: [],
  pendingCheckpoint: null,
  report: null,
  reports: [],
  loading: false,
  streaming: false,
  error: null,

  loadSessions: async () => {
    set({ sessionsLoading: true });
    try {
      const sessions = await listResearchSessions();
      set({ sessions });
    } catch (err) {
      set({ error: errorMessage(err) });
    } finally {
      set({ sessionsLoading: false });
    }
  },

  deleteSession: async (sessionId) => {
    set({ error: null });
    try {
      await apiDeleteResearchSession(sessionId);
      // 服务端确认**之后**才从本地移除（不做乐观移除）：删除失败必须让列表保持原样，
      // 否则用户会以为删掉了、刷新又回来。
      set((state) => ({
        sessions: state.sessions.filter((session) => session.id !== sessionId),
      }));
    } catch (err) {
      set({ error: errorMessage(err) });
      throw err;
    }
  },

  openSession: async (sessionId) => {
    set({ loading: true });
    try {
      const detail = await getResearchSession(sessionId);
      set({
        currentSession: detail.session,
        turns: detail.turns,
        pendingCheckpoint: detail.pendingCheckpoint,
        events: [],
        report: null,
        error: null,
      });
    } catch (err) {
      set({ error: errorMessage(err) });
    } finally {
      set({ loading: false });
    }
  },

  sendQuestion: async (question, mode = "research") => {
    set({ loading: true, error: null });
    try {
      const session = await createResearchSession({ question, mode });
      set({
        currentSession: session,
        turns: [],
        events: [],
        pendingCheckpoint: null,
        report: null,
      });
      // 起首轮：状态机后台跑，进度走 SSE。流编排（先 connectStream 再提交 turn）
      // 由调用方负责 —— 后端不做历史回放，故 store 不在这里隐式建流。
      await get().submitTurn(session.id, question);
      return session.id;
    } catch (err) {
      set({ error: errorMessage(err) });
      throw err;
    } finally {
      set({ loading: false });
    }
  },

  submitTurn: async (sessionId, question) => {
    set({ error: null });
    try {
      await apiSubmitTurn(sessionId, question);
    } catch (err) {
      set({ error: errorMessage(err) });
      throw err;
    }
  },

  answer: async (checkpointId, action, choice) => {
    set({ error: null });
    try {
      await apiAnswerCheckpoint(checkpointId, { action, choice: choice ?? {} });
      // 决策已提交：无论状态机接下来如何走，pendingCheckpoint 都不再是待决策点。
      set({ pendingCheckpoint: null });
    } catch (err) {
      set({ error: errorMessage(err) });
      throw err;
    }
  },

  loadReport: async (sessionId, version) => {
    set({ error: null });
    try {
      const report = await apiGetReport(sessionId, version);
      set({ report });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  loadReports: async (sessionId) => {
    set({ error: null });
    try {
      const reports = await apiListReports(sessionId);
      set({ reports });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  connectStream: async (sessionId, signal, onOpen) => {
    // 断旧流再建新流：避免旧事件继续追加进新会话。
    activeAbort?.abort();
    const controller = new AbortController();
    activeAbort = controller;
    const forwardAbort = () => controller.abort();
    signal?.addEventListener("abort", forwardAbort, { once: true });

    // 开流信号只发一次：api 在 fetch 返回 ok 后触发，store 在 error/abort 路径兜底
    // 触发。二者可能重叠（开流成功后又因断流而读流失败），保证调用方只被通知一次。
    let openSignalled = false;
    const signalOpen = () => {
      if (openSignalled) return;
      openSignalled = true;
      onOpen?.();
    };

    set({ streaming: true, error: null });
    try {
      await openResearchStream(
        sessionId,
        (event) => {
          set((state) => applyEvent(state, event));
        },
        controller.signal,
        signalOpen,
      );
    } catch (err) {
      // 主动断流（组件卸载 / 切会话 / 外部 signal）不算错误；但无论成功与否都必须
      // signalOpen，否则串行编排的调用方（先等 onOpen 再提交首轮）会永久挂起。
      if (controller.signal.aborted) {
        signalOpen();
        return;
      }
      signalOpen();
      set({ error: errorMessage(err) });
    } finally {
      signal?.removeEventListener("abort", forwardAbort);
      if (activeAbort === controller) {
        activeAbort = null;
        set({ streaming: false });
      }
    }
  },

  reset: () => {
    activeAbort?.abort();
    activeAbort = null;
    set({
      sessions: [],
      currentSession: null,
      turns: [],
      events: [],
      pendingCheckpoint: null,
      report: null,
      reports: [],
      loading: false,
      streaming: false,
      error: null,
    });
  },
}));
