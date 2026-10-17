// 研究型 Agent 入口会话状态（feat-research-entry Task 9）。
//
// 独立 store，不复用全局 chatStore（chat 与 doc_qa 共用一个 store 导致消息状态
// 互相污染的教训）。进度经独立 SSE 端点（research.* 事件族）流入，按事件名分发
// （事件名本身就是语义，不能像 wikiChat 那样压成 kind）。

import { create } from "zustand";
import { i18n } from "../i18n";
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
// B3.5: 对外暴露 applyResearchEvent + buildInitialState 供 unit test 直接调用。
//
// buildInitialState 只返回数据字段（不带 actions）—— ResearchState 是数据+actions
// 的合集，buildInitialState 在测试里被当作纯数据用；tsc -b 严格模式拒绝返回残缺
// ResearchState。ResearchDataState 是数据子集，vitest 用 vite-node 转译（不跑严格
// tsc）所以这个错误先前未暴露；npm run build 跑 `tsc -b` 立刻炸。
export function buildInitialState(): ResearchDataState {
  return {
    sessions: [],
    sessionsLoading: false,
    currentSession: null,
    turns: [],
    events: [],
    pendingCheckpoint: null,
    resolutionInFlight: false,
    report: null,
    reports: [],
    loading: false,
    streaming: false,
    error: null,
    conflictError: null,
    degradedNotice: null,
  };
}

export function applyResearchEvent(
  state: ResearchDataState,
  event: ResearchSseEvent,
): Partial<ResearchDataState> {
  const events = [...state.events, event];
  switch (event.name) {
    case "research.checkpoint":
      // 新检查点 = 上一题 answer 落地 → 解除 resolutionInFlight，让按钮恢复可点
      return { events, pendingCheckpoint: checkpointFromEvent(event.payload), resolutionInFlight: false };
    case "research.done":
      // 研究彻底跑完：清 streaming 供「分析完成」通知 trigger 用；同时清 resolutionInFlight。
      return { events, streaming: false, resolutionInFlight: false };
    case "research.error": {
      // 处置只按 uiHint 的「类」分支，绝不按 code 逐个判断（用户裁定规则 ③）。
      if (isTerminalError(event.payload)) {
        return {
          events,
          streaming: false,
          resolutionInFlight: false,
          error: typeof event.payload.message === "string" ? event.payload.message : "research.error",
        };
      }
      // B3.5: 并发 checkpoint 决策冲突。流关闭 + 暴露 conflictError 供页面 toast。
      if (event.payload.uiHint === "conflict") {
        const message =
          typeof event.payload.message === "string" && event.payload.message.length > 0
            ? event.payload.message
            : "research.error.checkpoint_conflict";
        return {
          events,
          streaming: false,
          resolutionInFlight: false,
          error: message,
          conflictError: message,
        };
      }
      // 降级类（degraded）：流保持打开，状态机会继续推进，后面可能还有 done。
      // 同时落 degradedNotice —— 这是 degraded 类**唯一**的 UI 面（主区常驻 Alert）。
      // 分支按 uiHint 的**类**，不按 code（规则 ③）：`budget_metering_degraded` 之外的
      // 既有 degraded code（llm_unavailable / step_failed / sql_validation_failed /
      // hypothesis_generation_failed）也走这里 —— 它们此前对用户完全静默，本次**统一**
      // 补上这个面；文案一律取后端 message（各 code 各自的措辞），前端不写 code 判断。
      if (event.payload.uiHint === "degraded") {
        return {
          events,
          // 兜底必须是**已翻译**文案：此处不进 t() 渲染（页面 Alert 直渲 message），
          // 写 i18n key 会让用户看到字面 key（与 ResearchProgress 走 t() 的口径不一致）。
          degradedNotice:
            typeof event.payload.message === "string" && event.payload.message.length > 0
              ? event.payload.message
              : i18n.t("research.progress.degraded"),
        };
      }
      // 其余（uiHint 缺失 / 未来未知取值）：只追加事件，不关流、不设提示（fail-safe）。
      return { events };
    }
    default:
      // finding / report / step.* / intent / plan / esl / hypothesis 等中间帧：
      // 上一题 answer 还在后端消化中（等着下一次 checkpoint 落地）。**保留**
      // resolutionInFlight，让 CheckpointCard 仍显示「处理中…」banner + 按钮 disabled，
      // 直到新 research.checkpoint 帧到达（新检查点 = 真正可以再答了）。
      // （2026-10-09 bug：早期版本 default 也清，banner 闪一下就消失 + 通知 trigger
      //   因 resolutionInFlight 闸挡着不弹。后续真根因在 ResearchSessionPage trigger
      //   上：见 trigger 注释。）
      return { events };
  }
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// --- SSE 重连游标（Task 12b） -------------------------------------------------
// 每个会话最后收到的事件 `seq` 存 sessionStorage（**传输层游标**，不是 UI state：
// 刻意不放进 zustand 持久化 state）。重连时带上 `?lastEventId=`，后端回放错过的事件。
const LAST_EVENT_ID_PREFIX = "research:lastEventId:";

function lastEventIdKey(sessionId: string): string {
  return `${LAST_EVENT_ID_PREFIX}${sessionId}`;
}

/** 读游标；读失败（SSR / jsdom 无 sessionStorage / 隐私模式）当**无游标**，不抛。 */
function readStoredLastEventId(key: string): number | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (raw === null) return null;
    const parsed = Number(raw);
    return Number.isInteger(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** 写游标；写失败只在控制台留痕，**不抛** —— 回放失败不该影响实时流。 */
function writeStoredLastEventId(key: string, seq: number): void {
  try {
    sessionStorage.setItem(key, String(seq));
  } catch (err) {
    console.warn("[research] 事件游标写入 sessionStorage 失败：", err);
  }
}

interface ResearchDataState {
  sessions: ResearchSession[];
  sessionsLoading: boolean;
  currentSession: ResearchSession | null;
  turns: ResearchTurn[];
  events: ResearchSseEvent[];
  pendingCheckpoint: ResearchCheckpoint | null;
  resolutionInFlight: boolean;
  report: ResearchReport | null;
  reports: ResearchReportSummary[];
  loading: boolean;
  streaming: boolean;
  error: string | null;
  conflictError: string | null;
  /** 降级类 `research.error` 的可见信号（§3.2 预算闸门静默失效）。后端 fail-open 放行
   *  本轮但用户必须看得见 —— 与 `error` 同生命周期：开新流即清，流内不清（降级期间
   *  状态机仍在推进，提示要一直挂着，直到下一轮开始）。 */
  degradedNotice: string | null;
}

interface ResearchState extends ResearchDataState {
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
  clearConflictError: () => void;
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
  resolutionInFlight: false,
  report: null,
  reports: [],
  loading: false,
  streaming: false,
  error: null,
  conflictError: null,
  degradedNotice: null,

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
      // 删除失败的反馈由调用方（列表页 message.error）负责：这里**不写**共享 error 槽，
      // 否则该文案会串到另一个已打开会话页的 Alert（openSession 仅在成功分支清 error）。
      // 开头的 set({ error: null }) 保留——它保证本动作不把陈旧错误留在槽里。
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
        // 跨会话必须清：切到一个**已结束**会话时不会建流（useResearchSession 对终态会话
        // 直接 return），若不清，上一会话的降级横幅会原样挂在新会话页面上（误导）。
        degradedNotice: null,
      });
    } catch (err) {
      set({ error: errorMessage(err) });
    } finally {
      set({ loading: false });
    }
  },

  sendQuestion: async (question, mode = "research") => {
    set({ loading: true, error: null, degradedNotice: null });
    try {
      const session = await createResearchSession({ question, mode });
      set({
        currentSession: session,
        turns: [],
        events: [],
        pendingCheckpoint: null,
        report: null,
        degradedNotice: null,
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
      // B4: 不立即清空 pendingCheckpoint；等下一个 SSE 事件到达再切换。
      // resolutionInFlight=true 表示已提交答案、等待确认中。
      set({ resolutionInFlight: true });
    } catch (err) {
      set({ error: errorMessage(err), resolutionInFlight: false });
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

    // 新流开始 = 新一轮：清上一轮的降级提示（与 error 同生命周期）
    set({ streaming: true, error: null, degradedNotice: null });
    const cursorKey = lastEventIdKey(sessionId);
    // 去重游标（本次连接内单调递增）：后端**先订阅再回放**，落在窗口内的事件会被投递
    // 两次 ⇒ `event.id <= lastSeq` 的事件丢弃。`id == null`（后端未落库）无 seq 可判，
    // 照常透传（这一缺口无法在本层消除）。
    let lastSeq: number | null = null;
    try {
      await openResearchStream(
        sessionId,
        (event) => {
          if (event.id != null) {
            if (lastSeq !== null && event.id <= lastSeq) return; // 重复帧：丢弃
            lastSeq = event.id;
            writeStoredLastEventId(cursorKey, event.id);
          }
          set((state) => applyResearchEvent(state, event));
        },
        controller.signal,
        signalOpen,
        readStoredLastEventId(cursorKey),
      );
    } catch (err) {
      // 主动断流（组件卸载 / 切会话 / 外部 signal）不算错误；但无论成功与否都必须
      // signalOpen，否则串行编排的调用方（先等 onOpen 再提交首轮）会永久挂起。
      if (controller.signal.aborted) {
        signalOpen();
        return;
      }
      signalOpen();
      set({ error: errorMessage(err), resolutionInFlight: false });
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
      sessionsLoading: false, // ← 重置 loading 旗：否则下个 case 的 useEffect 看
      // sessions=[] 但 sessionsLoading=true，永远不调 loadSessions（test isolation bug）
      currentSession: null,
      turns: [],
      events: [],
      pendingCheckpoint: null,
      resolutionInFlight: false,
      report: null,
      reports: [],
      loading: false,
      streaming: false,
      error: null,
      conflictError: null,
      degradedNotice: null,
    });
  },

  clearConflictError: () => set({ conflictError: null }),
}));
