import { create } from "zustand";
import {
  sendMessage as sendChatMessage,
  sendMessageStream,
  type StreamChartData,
} from "../api/chat";
import {
  deleteSessionHistory as apiDeleteSession,
  listChatSessions as apiListChatSessions,
  loadSessionMessages as apiLoadSessionMessages,
} from "../api/chatHistory";
import type {
  ChatMessageRead,
  ChatSession,
  SessionMessagesResponse,
} from "../types/chatHistory";
import type { ChatMessage, ChartType, HistoryMessage, IntentType, MultiStepStep } from "../types/chat";
import { i18n } from "../i18n";
import { read as readPersisted, write as writePersisted } from "./persistChatUiState";

// 后端已知意图集合（用于运行时收窄 meta 事件，未知值不入库）
// 9 个活跃值：query 系列 + 本体治理指令（define/map/metric，Phase 2 接入）
const KNOWN_INTENTS = new Set<IntentType>([
  "query",
  "chitchat",
  "refine",
  "follow_up",
  "new_query",
  "clarify",
  "define",
  "map",
  "metric",
  // 拦截路径意图（Phase 5.3/5.4/6.3）：卡片按字段存在性渲染，但意图需可持久化
  "supplier_360",
  "supplier_risk",
  "graph_reasoning",
]);

function isIntent(value: unknown): value is IntentType {
  return typeof value === "string" && KNOWN_INTENTS.has(value as IntentType);
}

// 会话回传的历史消息上限（20 条 ≈ 10 轮）
const HISTORY_LIMIT = 20;

let idCounter = 0;

function nextId(): string {
  idCounter += 1;
  return `m-${idCounter}-${Date.now()}`;
}

export function generateSessionId(): string {
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function toHistory(messages: ChatMessage[]): HistoryMessage[] {
  return messages.slice(-HISTORY_LIMIT).map((m) => ({ role: m.role, content: m.content }));
}

// 流式占位：以新增对象替换最后一条消息（不可变更新，遵循全局编码规范）
function patchLastMessage(messages: ChatMessage[], patch: Partial<ChatMessage>): ChatMessage[] {
  const last = messages[messages.length - 1];
  if (!last) return messages;
  return [...messages.slice(0, -1), { ...last, ...patch }];
}

// 不可变更新最后一条助手消息中指定 stepIndex 的步骤（不动其它步骤与消息）
function patchStep(messages: ChatMessage[], stepIndex: number, patch: Partial<MultiStepStep>): ChatMessage[] {
  const last = messages[messages.length - 1];
  if (!last) return messages;
  const steps = last.steps ?? [];
  const nextSteps = steps.map((s) => (s.stepIndex === stepIndex ? { ...s, ...patch } : s));
  return patchLastMessage(messages, { steps: nextSteps });
}

// 流式结束：把仍处于「执行中」的步骤标记为「已完成」（汇总步骤无 step_result，靠 done 收尾）
function finalizeRunningSteps(messages: ChatMessage[]): ChatMessage[] {
  const last = messages[messages.length - 1];
  if (!last?.steps?.some((s) => s.status === "running")) return messages;
  return patchLastMessage(messages, {
    steps: last.steps.map((s) => (s.status === "running" ? { ...s, status: "done" as const } : s)),
  });
}

// 历史会话消息 → 前端 ChatMessage 转换（chart/chartOption/data 未持久化，历史回放仅展示文本与 SQL）
function toChatMessage(read: ChatMessageRead): ChatMessage {
  const ts = Date.parse(read.createdTime);
  return {
    id: `m-history-${read.id}`,
    role: read.role,
    content: read.content,
    timestamp: Number.isFinite(ts) ? ts : Date.now(),
    sql: read.sql,
    isStreaming: false,
  };
}

interface ChatState {
  messages: ChatMessage[];
  sessionId: string;
  loading: boolean;
  datasourceId: number | null;
  selectedModelId: number | null;
  error: string | null;
  // 历史会话面板（feat-chat-history-panel）
  sessions: ChatSession[];
  sessionsLoading: boolean;
  sessionsError: string | null;
  historyPanelOpen: boolean;
  setDatasourceId: (id: number | null) => void;
  setSelectedModelId: (id: number | null) => void;
  addMessage: (msg: ChatMessage) => void;
  sendMessage: (question: string, useStream?: boolean, chartType?: ChartType | null) => Promise<void>;
  clearMessages: () => void;
  resetSession: () => void;
  // 历史会话面板 actions
  loadSessions: () => Promise<void>;
  loadSessionMessages: (sessionId: string) => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;
  toggleHistoryPanel: () => void;
  setHistoryPanelOpen: (open: boolean) => void;
}

// 初始化时一次性读取 localStorage（hydration）。后续切换不重复读，
// 因此 store 之外的模块如需响应变化应订阅 persistChatUiState 自身（本期不实现）
const persisted = readPersisted();

export const useChatStore = create<ChatState>()((set, get) => ({
  messages: [],
  sessionId: persisted.lastSessionId ?? generateSessionId(),
  loading: false,
  datasourceId: null,
  selectedModelId: null,
  error: null,
  sessions: [],
  sessionsLoading: false,
  sessionsError: null,
  historyPanelOpen: persisted.historyPanelOpen,

  setDatasourceId: (id) => set({ datasourceId: id }),

  setSelectedModelId: (id) => set({ selectedModelId: id }),

  addMessage: (msg) =>
    set((state) => ({ messages: [...state.messages, msg], error: null })),

  sendMessage: async (question, useStream = false, chartType = null) => {
    const { sessionId, messages, datasourceId, loading } = get();
    if (loading) return;
    if (!datasourceId) {
      set({ error: i18n.t("toast.pleaseSelectDatasource") });
      return;
    }

    // 先写入用户消息 + 流式占位助手消息
    const userMsg: ChatMessage = {
      id: nextId(),
      role: "user",
      content: question,
      timestamp: Date.now(),
    };
    const placeholderMsg: ChatMessage = {
      id: nextId(),
      role: "assistant",
      content: "",
      timestamp: Date.now(),
      isStreaming: true,
    };
    set((state) => ({
      messages: [...state.messages, userMsg, placeholderMsg],
      loading: true,
      error: null,
    }));

    const payload = {
      sessionId,
      question,
      datasourceId,
      history: toHistory(messages),
      modelId: get().selectedModelId,
      chartType,
    };

    try {
      if (useStream) {
        await sendMessageStream(payload, {
          onMeta: (intent) =>
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                // 运行时收窄：仅接受已知意图，未知值不入库
                intent: isIntent(intent) ? intent : undefined,
              }),
            })),
          // ReAct 查询计划（Phase E）：流式中已可回填，完成后配合 isStreaming=false 展示
          onPlan: (plan) =>
            set((state) => ({
              messages: patchLastMessage(state.messages, { queryPlan: plan }),
            })),
          onSql: (sql) =>
            set((state) => ({ messages: patchLastMessage(state.messages, { sql }) })),
          onChart: (chart: StreamChartData) =>
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                chartType: chart.chartType,
                chartOption: chart.chartOption,
                data: chart.data,
              }),
            })),
          // 完整计划概览：建立各步骤（含汇总步骤），初始状态「待执行」
          onStepPlanOverview: (steps) =>
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                steps: steps.map(
                  (s): MultiStepStep => ({
                    stepIndex: s.stepIndex,
                    description: s.description,
                    subQuestion: s.subQuestion,
                    aggregationOnly: s.aggregationOnly,
                    status: "pending",
                  })
                ),
              }),
            })),
          // 单个子步骤进入执行：标记「执行中」并高亮当前步骤
          onStepPlan: (step) =>
            set((state) => ({
              messages: patchLastMessage(
                patchStep(state.messages, step.stepIndex, { status: "running" }),
                { currentStepIndex: step.stepIndex }
              ),
            })),
          // 单个子步骤完成：标记「完成/失败」并回填 sql/summary/error
          onStepResult: (result) =>
            set((state) => ({
              messages: patchStep(state.messages, result.stepIndex, {
                status: result.error ? "error" : "done",
                sql: result.sql ?? null,
                summary: result.summary ?? null,
                error: result.error ?? null,
              }),
            })),
          // Phase 1.4：目标表可信度 badge（与 queryPlan 一起展示）
          // 用浅合并（不可变 patch）覆盖，避免后续事件把已有 badge 抹掉
          onDataQuality: (payload) =>
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                dataQuality: payload.badges,
              }),
            })),
          onToken: (content) =>
            set((state) => {
              const last = state.messages[state.messages.length - 1];
              return {
                messages: patchLastMessage(state.messages, {
                  content: (last.content ?? "") + content,
                }),
              };
            }),
          onDone: ({ tokensUsed, cost, modelName, affinityStatus }) =>
            set((state) => ({
              messages: finalizeRunningSteps(
                patchLastMessage(state.messages, {
                  tokensUsed,
                  cost,
                  modelName: modelName ?? undefined,
                  isStreaming: false,
                  affinityStatus: affinityStatus ?? null,
                })
              ),
              loading: false,
            })),
          onError: (message, detail) =>
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                content: message,
                errorDetail: detail ?? null,
                isError: true,
                isStreaming: false,
              }),
              loading: false,
              error: message,
            })),
        });
      } else {
        const res = await sendChatMessage(payload);
        set((state) => ({
          messages: patchLastMessage(state.messages, {
            content: res.answer,
            sql: res.sql ?? null,
            chartType: res.chartType ?? null,
            chartOption: res.chartOption ?? null,
            data: res.data ?? null,
            queryPlan: res.queryPlan ?? null,
            intent: isIntent(res.intent) ? res.intent : undefined,
            extractedEntities: res.extractedEntities ?? null,
            tokensUsed: res.tokensUsed,
            cost: res.cost,
            modelName: res.modelName ?? undefined,
            isStreaming: false,
            affinityStatus: res.affinityStatus ?? null,
            // Phase 1.4：DQ 可信度 badge（顺序对齐 queryPlan.selectedClasses）
            dataQuality: res.dataQuality ?? null,
            // 非流式多步：steps 数组均为「已完成」（后端仅回传数据步骤，无汇总步骤）
            steps: res.steps?.map(
              (s): MultiStepStep => ({
                stepIndex: s.stepIndex,
                description: s.description,
                subQuestion: s.subQuestion,
                aggregationOnly: false,
                status: "done",
                sql: s.sql ?? null,
                summary: s.summary ?? null,
                error: s.error ?? null,
              })
            ),
          }),
          loading: false,
        }));
      }
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : i18n.t("errors.networkError");
      const errDetail = (err as { detail?: string }).detail ?? null;
      set((state) => {
        const last = state.messages[state.messages.length - 1];
        // 已通过 error 事件展示具体错误时，不覆盖为通用错误文案（LOW#8 修复）
        if (last?.isError) {
          return { loading: false };
        }
        return {
          messages: patchLastMessage(state.messages, {
            content: errMsg,
            errorDetail: errDetail,
            isError: true,
            isStreaming: false,
          }),
          loading: false,
          error: errMsg,
        };
      });
    } finally {
      // 兜底复位：若流异常结束（无 done/error 帧）导致 loading / isStreaming 残留，
      // 强制复位，避免发送按钮永久禁用（HIGH#3 修复）
      set((state) => {
        const last = state.messages[state.messages.length - 1];
        if (!state.loading && !last?.isStreaming) return {};
        if (!last?.isStreaming) return { loading: false };
        return {
          messages: patchLastMessage(state.messages, { isStreaming: false }),
          loading: false,
        };
      });
    }
  },

  clearMessages: () => set({ messages: [], error: null }),

  resetSession: () => {
    const newId = generateSessionId();
    writePersisted({ lastSessionId: newId });
    set({
      messages: [],
      sessionId: newId,
      loading: false,
      error: null,
    });
  },

  // ============ 历史会话面板 actions ============

  loadSessions: async () => {
    set({ sessionsLoading: true, sessionsError: null });
    try {
      const sessions = await apiListChatSessions();
      set({ sessions, sessionsLoading: false });
    } catch (err) {
      const msg = err instanceof Error ? err.message : i18n.t("errors.networkError");
      set({ sessionsError: msg, sessionsLoading: false });
    }
  },

  loadSessionMessages: async (sessionId) => {
    try {
      const resp: SessionMessagesResponse = await apiLoadSessionMessages(sessionId);
      const messages = resp.messages.map(toChatMessage);
      writePersisted({ lastSessionId: sessionId });
      set({
        sessionId,
        messages,
        loading: false,
        error: null,
        sessionsError: null,
      });
    } catch (err) {
      // 加载失败写入 sessionsError（用户可见于面板 Alert），不污染 chat 区 error
      // —— chat 区错误展示与历史面板错误展示语义解耦，避免互相覆盖
      const msg = err instanceof Error ? err.message : i18n.t("errors.networkError");
      set({ sessionsError: msg });
    }
  },

  deleteSession: async (sessionId) => {
    await apiDeleteSession(sessionId);
    // 不可变移除 sessions 中对应项；若是当前会话则同时清空 messages 与 sessionId
    set((state) => {
      const nextSessions = state.sessions.filter((s) => s.sessionId !== sessionId);
      if (state.sessionId !== sessionId) {
        return { sessions: nextSessions };
      }
      const newId = generateSessionId();
      writePersisted({ lastSessionId: newId });
      return {
        sessions: nextSessions,
        sessionId: newId,
        messages: [],
        loading: false,
        error: null,
      };
    });
  },

  toggleHistoryPanel: () => {
    const next = !get().historyPanelOpen;
    writePersisted({ historyPanelOpen: next });
    set({ historyPanelOpen: next });
  },

  setHistoryPanelOpen: (open) => {
    writePersisted({ historyPanelOpen: open });
    set({ historyPanelOpen: open });
  },
}));
