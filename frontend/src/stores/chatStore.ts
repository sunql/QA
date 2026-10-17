import { create, type StoreApi } from "zustand";
import { v4 as uuidv4 } from "uuid";
import {
  fetchHypotheses as apiFetchHypotheses,
  resumeMultiStepRun,
  sendMessage as sendChatMessage,
  sendMessageStream,
  type StreamChartData,
  type StreamEventHandlers,
} from "../api/chat";
import { searchDocumentsQa } from "../api/document";
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
import type {
  ChatMessage,
  ChartType,
  HistoryMessage,
  IntentType,
  MultiStepStep,
  StepStatus,
} from "../types/chat";
import { asChartOption, asTablePayload, asVisualRationale, normalizeChartType } from "../utils/chartContract";
import { i18n } from "../i18n";
import {
  readHistoryPanelOpen,
  readLastChannel,
  readLastSessionId,
  writeHistoryPanelOpen,
  writeLastChannel,
  writeLastSessionId,
  type ChatChannel,
} from "./persistChatUiState";
import { onUserSwitch } from "./userSwitch";

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
  // Phase 6.4：Agent 运行时（AGENT_RUN 意图最优先，显式指名 Agent）
  "agent_run",
]);

function isIntent(value: unknown): value is IntentType {
  return typeof value === "string" && KNOWN_INTENTS.has(value as IntentType);
}

// 会话回传的历史消息上限（20 条 ≈ 10 轮）
const HISTORY_LIMIT = 20;
// 后端 fetch 断开 / EventSource 关闭时，浏览器 FetchError.message 常含此字样
// （与 line 206/846 P1-3 注释里的 "stream interrupted" 口径一致）
const STREAM_INTERRUPTED_DETECTION = "interrupted";

let idCounter = 0;

function nextId(): string {
  idCounter += 1;
  return `m-${idCounter}-${Date.now()}`;
}

export function generateSessionId(): string {
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function makeSessionId(channel: ChatChannel): string {
  return `${channel === "doc_qa" ? "docqa-" : "chat-"}${crypto.randomUUID()}`;
}

// 回放代际：异步回放只允许**最新一次**落地。
//
// 没有它时的两个真实故障（都属于「晚到的响应盖掉当前状态」）：
//   1. 进 /chat 触发的 tail 回放还在路上，用户切到「文档问答」→ 回放回来把 chat 的
//      消息与会话 id 盖到 doc_qa 面板上（跨渠道串台），接着发问就会带着 chat 的 id；
//   2. 发送过程中回放到达 → `set` 整体替换 messages，冲掉刚入队的用户消息与流式占位，
//      并把 loading 置回 false，使 sendMessage 的 `if (loading) return` 防线失效。
//
// 凡「要重新决定屏幕上显示哪个会话」的动作都调 invalidatePendingLoads()：切渠道、
// 点历史项、新建会话、删除会话、发送新消息、换人。
let sessionLoadSeq = 0;

function invalidatePendingLoads(): void {
  sessionLoadSeq += 1;
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

// 定向更新：targetId 为空时退化为「更新最后一条」（首发路径行为不变）。
//
// 续跑必须用它：SSE handler 默认写「最后一条」，而续跑的目标卡片可能是历史里的
// 任意一条（用户上滚点旧卡片的续跑）—— 写 last 会让被点的卡片纹丝不动、
// 把最新的消息污染掉。找不到 id 时 map 天然是恒等变换（不新增、不报错）。
function patchMessage(
  messages: ChatMessage[],
  targetId: string | undefined,
  patch: Partial<ChatMessage>
): ChatMessage[] {
  if (!targetId) return patchLastMessage(messages, patch);
  return messages.map((m) => (m.id === targetId ? { ...m, ...patch } : m));
}

// 后端步骤结果 → 前端终态：有 error 即失败（失败步骤 sql 恒为 null，与后端同判据）。
// 流式（onStepResult）与非流式（响应 steps 回填）共用，避免两处判据漂移。
function stepStatusFromResult(error: string | null | undefined): StepStatus {
  return error ? "error" : "done";
}

// 不可变更新指定消息（targetId 为空 ⇒ 最后一条）中指定 stepIndex 的步骤
// （不动其它步骤与消息）
function patchStep(
  messages: ChatMessage[],
  targetId: string | undefined,
  stepIndex: number,
  patch: Partial<MultiStepStep>
): ChatMessage[] {
  // 取数也必须认 targetId：否则定向写会把**最新**消息的 steps 搬到旧卡片上
  const target = targetId
    ? messages.find((m) => m.id === targetId)
    : messages[messages.length - 1];
  if (!target) return messages;
  const steps = target.steps ?? [];
  const nextSteps = steps.map((s) => (s.stepIndex === stepIndex ? { ...s, ...patch } : s));
  return patchMessage(messages, targetId, { steps: nextSteps });
}

// 流式结束：把仍处于「执行中」的步骤标记为「已完成」（汇总步骤无 step_result，靠 done 收尾）
// targetId 为空 ⇒ 最后一条（首发行为不变）；续跑时必须指向目标卡片，
// 否则旧卡片里卡在「执行中」的汇总步永远不会收尾。
function finalizeRunningSteps(messages: ChatMessage[], targetId?: string): ChatMessage[] {
  const target = targetId
    ? messages.find((m) => m.id === targetId)
    : messages[messages.length - 1];
  if (!target?.steps?.some((s) => s.status === "running")) return messages;
  return patchMessage(messages, targetId, {
    steps: target.steps.map((s) => (s.status === "running" ? { ...s, status: "done" as const } : s)),
  });
}

// 历史会话消息 → 前端 ChatMessage 转换。
// 0105 起 chart/chartOption 已持久化，历史回放也能出图（此前只展示文本与 SQL）；
// `data` 仍未落库 —— 图/表由 chartOption 自带（TABLE 的 rows 在里面），不需要它。
function toChatMessage(read: ChatMessageRead): ChatMessage {
  const ts = Date.parse(read.createdTime);
  return {
    id: `m-history-${read.id}`,
    role: read.role,
    content: read.content,
    timestamp: Number.isFinite(ts) ? ts : Date.now(),
    sql: read.sql,
    isStreaming: false,
    // H4：断连兜底写入的半截回答，UI 据此提示「内容不完整」
    interrupted: read.interrupted,
    // 系统边界收窄：落库的 kind 可能来自更早版本的后端，白名单不认识就置 null
    // （渲染门不放行，等于「这轮没图」），而不是让未知类型流进渲染层。
    chartType: normalizeChartType(read.chartType),
    chartOption: asChartOption(read.chartOption),
    // 0107：明细表负载 + 判断依据同是系统边界原始 JSON，与 chartType/chartOption
    // 同一道收窄（older 后端可能发来未知形状，收窄成 null 等于「这轮没有」）。
    tableOption: asTablePayload(read.tableOption),
    visualRationale: asVisualRationale(read.visualRationale),
    // 回放出来的消息天然知道自己的落库主键，补上它「导出此条」按钮才会出现。
    // 此前不填：入口只在 dbMessageId 存在时才渲染，于是刷新恢复出来的会话
    // 整段没有单条导出（实时发送的消息仍然没有，见 ChatMessage.dbMessageId 注释）。
    dbMessageId: read.id,
    // F2：kpiDatasourceCode 不在后端 DTO 里，会话恢复时显式清零（不靠「新对象恰好没这个字段」的隐性保证）
    kpiDatasourceCode: null,
  };
}

/** 回放结果：成功落地 / 载不动 / 被更新的动作作废（见 `sessionLoadSeq`）。 */
export type SessionLoadOutcome = "loaded" | "failed" | "stale";

interface ChatState {
  messages: ChatMessage[];
  sessionId: string;
  loading: boolean;
  datasourceId: number | null;
  selectedModelId: number | null;
  error: string | null;
  // P1-3：当前 placeholder 助手消息 id（流式结束后置 null；切会话/重发时旧 id 标 aborted）
  currentAssistantMessageId: string | null;
  // P1-3：EOF 守门设 true（stream interrupted: no done/error before EOF）
  interrupted: boolean;
  // 历史会话面板（feat-chat-history-panel）
  sessions: ChatSession[];
  sessionsLoading: boolean;
  sessionsError: string | null;
  historyPanelOpen: boolean;
  // Doc-Qa channel（documents-knowledge-qa, Task 7）
  channel: ChatChannel;
  /**
   * 面板挂载时声明「我属于哪个渠道」（ChatPage → chat，DocumentQaPanel → doc_qa）。
   *
   * 取代旧的 setChannel：旧实现无条件生成新 session id 且不清 messages
   * （跨渠道串台），也让「挂载时声明」冲掉要恢复的目标。新语义：
   *   - 渠道相同且已有消息 → 空操作（返回原处不重复拉取）
   *   - 渠道变了 → 换会话（messages 清空、取该渠道自己的恢复指针、错误清空）
   *   - 该渠道有恢复指针而当前没有消息 → 异步回放（刷新恢复）
   */
  enterChannel: (channel: ChatChannel) => Promise<void>;
  sendDocQa: (question: string, filters: { securityLevel?: string; documentType?: string }) => Promise<void>;
  setDatasourceId: (id: number | null) => void;
  setSelectedModelId: (id: number | null) => void;
  addMessage: (msg: ChatMessage) => void;
  sendMessage: (question: string, useStream?: boolean, chartType?: ChartType | null) => Promise<void>;
  /**
   * 续跑一个失败的多步 run（spec §7）：从 fromStepIndex 起重跑，响应同样是 SSE 流。
   *
   * 与首发共用 `streamHandlers`（同一套状态判据），只是入口不同。
   *
   * `messageId` 是**被点的那个卡片**的消息 id：续跑的事件必须写回它，
   * 而不是「最后一条」——否则用户上滚点旧卡片续跑时，旧卡片纹丝不动、
   * 最新的消息被污染。
   */
  resumeRun: (runId: string, fromStepIndex: number, messageId: string) => Promise<void>;
  clearMessages: () => void;
  resetSession: () => void;
  /**
   * 换人（登录 / 登出 / 401 掉线）时清空会话内存。
   *
   * 与 `resetSession` 的区别：**不动渠道**（渠道是 UI 位置，与身份无关），但必须换一个
   * 新 sessionId —— store 里的 id 与模块级 boot 快照都属于上一个人，沿用它会以别人的
   * 会话发问（对存量未打标会话服务端会放行，等于继承别人的追问锚点）。
   */
  clearForUserSwitch: () => void;
  // 历史会话面板 actions
  loadSessions: (channel?: ChatChannel) => Promise<void>;
  /**
   * 载入某会话的消息流。`tail: true` 取**最新**那批 ——
   * 刷新恢复必须用它：默认取的是会话开头，长会话会「恢复出开头、中间断掉」。
   *
   * 返回值：`loaded` 成功落地 / `failed` 载不动（指针已清，调用方应换一个干净会话）/
   * `stale` 期间已有更新的会话动作，本次结果被丢弃（调用方不插手）。
   */
  loadSessionMessages: (
    sessionId: string,
    options?: { tail?: boolean }
  ) => Promise<SessionLoadOutcome>;
  deleteSession: (sessionId: string) => Promise<void>;
  toggleHistoryPanel: () => void;
  setHistoryPanelOpen: (open: boolean) => void;
}

/**
 * 流式事件 → store 状态（首发与续跑共用同一套，避免两处状态判据漂移）。
 *
 * 抽取自 sendMessage 内联的 handlers 字面量：`resumeRun` 复用同一套判据，
 * 否则「续跑」的步骤状态与「首发」会各写一份、逐渐分叉。
 */
function streamHandlers(
  set: StoreApi<ChatState>["setState"],
  targetId?: string
): StreamEventHandlers {
  return {
    onMeta: (intent) =>
      set((state) => ({
        messages: patchMessage(state.messages, targetId, {
          // 运行时收窄：仅接受已知意图，未知值不入库
          intent: isIntent(intent) ? intent : undefined,
        }),
      })),
    // F2：KPI 数据源不一致警告 code（用于选 i18n 键，不存后端中文原文）
    onMetaWarning: (code) =>
      set((state) => ({
        messages: patchMessage(state.messages, targetId, { kpiDatasourceCode: code }),
      })),
    // ReAct 查询计划（Phase E）：流式中已可回填，完成后配合 isStreaming=false 展示
    onPlan: (plan) =>
      set((state) => ({
        messages: patchMessage(state.messages, targetId, { queryPlan: plan }),
      })),
    onSql: (sql) =>
      set((state) => ({ messages: patchMessage(state.messages, targetId, { sql }) })),
    onChart: (chart: StreamChartData) =>
      set((state) => ({
        messages: patchMessage(state.messages, targetId, {
          chartType: chart.chartType,
          chartOption: chart.chartOption,
          tableOption: chart.tableOption,
          visualRationale: chart.visualRationale,
          data: chart.data,
        }),
      })),
    // 完整计划概览：建立各步骤（含汇总步骤），初始状态「待执行」
    // runId 盖到每个步骤上：续跑按钮要凭它拼 resume 端点
    onStepPlanOverview: (steps, runId) =>
      set((state) => ({
        messages: patchMessage(state.messages, targetId, {
          steps: steps.map(
            (s): MultiStepStep => ({
              stepIndex: s.stepIndex,
              description: s.description,
              subQuestion: s.subQuestion,
              aggregationOnly: s.aggregationOnly,
              // F7/IMP-6：续跑时后端会为「已跳过（已持久化完成）」的步回放终态
              // （"done"）。不认它就一律回落成「待执行」——用户点续跑并跑成功后，
              // 前 N 个**已完成**的步反而显示「待执行」，看不出「续跑省掉了重跑」。
              // 缺省（普通新跑 / 旧后端）⇒ "pending"。
              status: s.status ?? "pending",
              runId,
            })
          ),
        }),
      })),
    // 单个子步骤进入执行：标记「执行中」并高亮当前步骤
    onStepPlan: (step) =>
      set((state) => ({
        messages: patchMessage(
          patchStep(state.messages, targetId, step.stepIndex, { status: "running" }),
          targetId,
          { currentStepIndex: step.stepIndex }
        ),
      })),
    // 单个子步骤完成：标记「完成/失败」并回填 sql/summary/error/图表
    onStepResult: (result) =>
      set((state) => ({
        messages: patchStep(state.messages, targetId, result.stepIndex, {
          status: stepStatusFromResult(result.error),
          sql: result.sql ?? null,
          summary: result.summary ?? null,
          error: result.error ?? null,
          chartType: result.chartType ?? null,
          chartOption: result.chartOption ?? null,
          tableOption: result.tableOption ?? null,
          visualRationale: result.visualRationale ?? null,
          queryPlan: result.queryPlan ?? null,
        }),
      })),
    // 更早的步被压缩：补上徽章（该步的 step_result 早已把它置为 done/error，
    // 这里覆盖成 compressed —— 压缩发生在它成功之后，覆盖是正确方向）
    onStepCompressed: (payload) =>
      set((state) => ({
        messages: patchStep(state.messages, targetId, payload.stepIndex, {
          status: "compressed",
          originalRows: payload.originalRows,
          compressedRows: payload.compressedRows,
        }),
      })),
    // Phase 1.4：目标表可信度 badge（与 queryPlan 一起展示）
    // 用浅合并（不可变 patch）覆盖，避免后续事件把已有 badge 抹掉
    onDataQuality: (payload) =>
      set((state) => ({
        messages: patchMessage(state.messages, targetId, {
          dataQuality: payload.badges,
        }),
      })),
    // 类召回诊断（2026-09-16）：截断/降级时 MessageItem 渲染提示
    onClassRecall: (info) =>
      set((state) => ({
        messages: patchMessage(state.messages, targetId, {
          classRecall: info,
        }),
      })),
    onToken: (content) =>
      set((state) => {
        // 读口也必须认 targetId：否则定向写会把**最新**那条的正文拼到旧卡片上（读/写分叉）。
        // base?.content：targetId 查不到那条时不能抛。
        const base = targetId
          ? state.messages.find((m) => m.id === targetId)
          : state.messages[state.messages.length - 1];
        return {
          messages: patchMessage(state.messages, targetId, {
            content: (base?.content ?? "") + content,
          }),
        };
      }),
    onDone: ({
      tokensUsed,
      cost,
      modelName,
      affinityStatus,
      agentRun,
      supplier360,
      supplierRisk,
      graphTraversal,
      suggestedAgent,
      queryPlan,
      visualRationale,
    }) =>
      set((state) => ({
        messages: finalizeRunningSteps(
          patchMessage(state.messages, targetId, {
            tokensUsed,
            cost,
            modelName: modelName ?? undefined,
            isStreaming: false,
            affinityStatus: affinityStatus ?? null,
            // #207 审查 HIGH 修复：流式 done 事件同样携带拦截类卡片对象
            //（此前仅非流式分支回填，导致默认 streaming UI 下卡片从未渲染）
            agentRun: agentRun ?? null,
            supplier360: supplier360 ?? null,
            supplierRisk: supplierRisk ?? null,
            graphTraversal: graphTraversal ?? null,
            // Phase 7 G4：中置信语义路由建议卡片随 done 帧回填
            suggestedAgent: suggestedAgent ?? null,
            // 顶层查询计划：多步时为最后一个成功数据步的计划，单步时直接来自响应
            queryPlan: queryPlan ?? null,
            // 0107：done 帧只在多步汇总/降级收尾携带 rationale（SUMMARY_TEXT_ONLY）；
            // 单步的 rationale 已由 chart 事件回填，这里只在非 null 时覆盖 ——
            // 否则会把 chart 事件写好的依据抹成 null（键必须整段缺省，不能 `?? undefined`）。
            ...(visualRationale ? { visualRationale } : {}),
          }),
          targetId
        ),
        loading: false,
      })),
    onError: (message, detail) =>
      set((state) => ({
        messages: patchMessage(state.messages, targetId, {
          content: message,
          errorDetail: detail ?? null,
          isError: true,
          isStreaming: false,
        }),
        loading: false,
        error: message,
      })),
  };
}

// 初始化时一次性读取 localStorage（hydration），把上次的渠道与它的会话指针摆好。
// **不在这里拉消息** —— 拉取要等面板声明渠道（enterChannel），因为「这次真的进的是哪个
// 渠道」只有面板知道，而回放失败的自愈（清指针）也在那边。
const bootChannel: ChatChannel = readLastChannel() ?? "chat";
const bootSessionId = readLastSessionId(bootChannel) ?? generateSessionId();

export const useChatStore = create<ChatState>()((set, get) => ({
  messages: [],
  sessionId: bootSessionId,
  loading: false,
  datasourceId: null,
  selectedModelId: null,
  error: null,
  currentAssistantMessageId: null,
  interrupted: false,
  sessions: [],
  sessionsLoading: false,
  sessionsError: null,
  historyPanelOpen: readHistoryPanelOpen(),
  channel: bootChannel,

  enterChannel: async (channel) => {
    const { channel: currentChannel, messages, sessionId } = get();
    const sameChannel = currentChannel === channel;
    // 同渠道且已有对话 → 回到原处，不重复拉取（面板重挂载 / 路由来回切）
    if (sameChannel && messages.length > 0) return;

    // 持久化的指针就是权威：它记的是「上次真的在这个渠道里说话的那个会话」。
    // 同渠道无指针时保留当前 id（别在每次挂载时无谓地换个新 id）。
    const pointer = readLastSessionId(channel);
    const nextId = pointer ?? (sameChannel ? sessionId : makeSessionId(channel));
    // 声明渠道即作废在途回放：上一条渠道的响应回来时不该再落到屏幕上
    invalidatePendingLoads();
    writeLastChannel(channel);
    // 同步部分先做完：sessionId 必须在进入的瞬间就是对的，不能等 await 回来
    set({
      channel,
      sessionId: nextId,
      // 只在真的换渠道时清空；同渠道重入要保留正在显示的那批消息。
      // sessions 一起清：历史面板已在显示时，换渠道前若不换列表，面板会短暂
      // 列出上一个渠道的会话（直到下次 loadSessions 回来）。
      ...(sameChannel ? {} : { messages: [], sessions: [] }),
      sessionsError: null,
      error: null,
    });

    if (pointer === null) return; // 没有可恢复的目标
    const outcome = await get().loadSessionMessages(pointer, { tail: true });
    if (outcome === "failed") {
      // 回放失败（网络 / 404 / 会话不属于当前用户）：恢复指针已由 loadSessionMessages
      // 清掉，这里再换一个干净会话。否则屏幕是空的、服务端却还留着那个 session 的
      // 追问锚点（session_query_state 只按 session_id 存），用户接着提问会得到
      // 「关于一场看不见的对话」的回答 —— 那比不恢复更坏。
      set({ sessionId: makeSessionId(channel) });
    }
    // "stale" = 期间用户已经切走 / 发了新消息，由更新的那次动作说了算，这里不插手
  },

  setDatasourceId: (id) => {
    if (get().datasourceId === id) return;  // P0-3：早返避免无谓 reset
    // 切数据源 = 全新会话（不允许旧 state 跨数据源继承）
    set({
      datasourceId: id,
      sessionId: generateSessionId(),
      messages: [],
      isStreaming: false,
    });
  },

  setSelectedModelId: (id) => set({ selectedModelId: id }),

  addMessage: (msg) =>
    set((state) => ({ messages: [...state.messages, msg], error: null })),

  sendMessage: async (question, useStream = false, chartType = null) => {
    const { sessionId, messages, datasourceId, loading, currentAssistantMessageId } = get();
    // P1-3：中止前一条还在进行中的流
    if (currentAssistantMessageId) {
      set((state) => ({
        messages: state.messages.map((m) =>
          m.assistantMessageId === currentAssistantMessageId
            ? { ...m, aborted: true, isStreaming: false }
            : m
        ),
      }));
    }
    if (loading) return;
    if (!datasourceId) {
      set({ error: i18n.t("toast.pleaseSelectDatasource") });
      return;
    }

    writeLastSessionId(get().channel, sessionId);
    invalidatePendingLoads();

    // P1-3：assistant id 立即分配
    const assistantMessageId = uuidv4();

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
      assistantMessageId, // P1-3
    };
    set((state) => ({
      messages: [...state.messages, userMsg, placeholderMsg],
      loading: true,
      error: null,
      currentAssistantMessageId: assistantMessageId,
      interrupted: false,
    }));

    const payload = {
      sessionId,
      question,
      datasourceId,
      history: toHistory(messages),
      modelId: get().selectedModelId,
      chartType,
    };

    // v3.1 B6（M7）：答案流结束（或非流式响应）后拉取「可能原因」假设。
    const attachHypotheses = async () => {
      if (get().channel !== "chat") return;
      try {
        const items = await apiFetchHypotheses(sessionId);
        if (!items.length) return;
        const current = items.filter((h) => h.turnQuestion === question);
        if (!current.length) return;
        set((state) => ({
          messages: patchLastMessage(state.messages, { hypotheses: current }),
        }));
      } catch {
        // 假设面板降级：失败不提示
      }
    };

    // P1-3：closure 守卫 —— 所有 12 handler 头加。
    // currentAssistantMessageId 为 null 时（即 sendMessageStream 被直接调用，非 chatStore.sendMessage），
    // 不拦截任何事件（让 chatApi.test.ts 的直接 SSE 测试能正常收到帧）。
    const isCurrent = () => {
      const id = get().currentAssistantMessageId;
      return id === null || id === assistantMessageId;
    };

    try {
      if (useStream) {
        await sendMessageStream(payload, {
          onMeta: (intent) => {
            if (!isCurrent()) return;
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                intent: isIntent(intent) ? intent : undefined,
              }),
            }));
          },
          onPlan: (plan) => {
            if (!isCurrent()) return;
            set((state) => ({
              messages: patchLastMessage(state.messages, { queryPlan: plan }),
            }));
          },
          onSql: (sql) => {
            if (!isCurrent()) return;
            set((state) => ({ messages: patchLastMessage(state.messages, { sql }) }));
          },
          onChart: (chart: StreamChartData) => {
            if (!isCurrent()) return;
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                chartType: chart.chartType,
                chartOption: chart.chartOption,
                tableOption: chart.tableOption,
                visualRationale: chart.visualRationale,
                data: chart.data,
              }),
            }));
          },
          onStepPlanOverview: (steps) => {
            if (!isCurrent()) return;
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
            }));
          },
          onStepPlan: (step) => {
            if (!isCurrent()) return;
            set((state) => ({
              messages: patchLastMessage(
                patchStep(state.messages, step.stepIndex, { status: "running" }),
                { currentStepIndex: step.stepIndex }
              ),
            }));
          },
          onStepResult: (result) => {
            if (!isCurrent()) return;
            set((state) => ({
              messages: patchStep(state.messages, result.stepIndex, {
                status: stepStatusFromResult(result.error),
                sql: result.sql ?? null,
                summary: result.summary ?? null,
                error: result.error ?? null,
                chartType: result.chartType ?? null,
                chartOption: result.chartOption ?? null,
                tableOption: result.tableOption ?? null,
                visualRationale: result.visualRationale ?? null,
                queryPlan: result.queryPlan ?? null,
              }),
            }));
          },
          onDataQuality: (payload) => {
            if (!isCurrent()) return;
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                dataQuality: payload.badges,
              }),
            }));
          },
          onClassRecall: (info) => {
            if (!isCurrent()) return;
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                classRecall: info,
              }),
            }));
          },
          onToken: (content) => {
            if (!isCurrent()) return;
            set((state) => {
              const last = state.messages[state.messages.length - 1];
              return {
                messages: patchLastMessage(state.messages, {
                  content: (last.content ?? "") + content,
                }),
              };
            });
          },
          onDone: ({
            tokensUsed,
            cost,
            modelName,
            affinityStatus,
            agentRun,
            supplier360,
            supplierRisk,
            graphTraversal,
            suggestedAgent,
            queryPlan,
            visualRationale,
          }) => {
            if (!isCurrent()) return;
            set((state) => ({
              messages: finalizeRunningSteps(
                patchLastMessage(state.messages, {
                  tokensUsed,
                  cost,
                  modelName: modelName ?? undefined,
                  isStreaming: false,
                  affinityStatus: affinityStatus ?? null,
                  agentRun: agentRun ?? null,
                  supplier360: supplier360 ?? null,
                  supplierRisk: supplierRisk ?? null,
                  graphTraversal: graphTraversal ?? null,
                  suggestedAgent: suggestedAgent ?? null,
                  queryPlan: queryPlan ?? null,
                  ...(visualRationale ? { visualRationale } : {}),
                })
              ),
              loading: false,
            }));
            void attachHypotheses();
          },
          onError: (message, detail) => {
            if (!isCurrent()) return;
            set((state) => ({
              messages: patchLastMessage(state.messages, {
                content: message,
                errorDetail: detail ?? null,
                isError: true,
                isStreaming: false,
              }),
              loading: false,
              error: message,
            }));
          },
        });
      } else {
      } else {
        // 非流式分支（不改；也要 reset currentAssistantMessageId）
        try {
          const response = await sendChatMessage(payload);
          set((state) => ({
            messages: patchLastMessage(state.messages, {
              content: response.answer,
              sql: response.sql ?? null,
              chartType: response.chartType ?? null,
              chartOption: response.chartOption ?? null,
              tableOption: response.tableOption ?? null,
              visualRationale: response.visualRationale ?? null,
              data: response.data ?? null,
              queryPlan: response.queryPlan ?? null,
              intent: isIntent(response.intent) ? response.intent : undefined,
              extractedEntities: response.extractedEntities ?? null,
              tokensUsed: response.tokensUsed,
              cost: response.cost,
              modelName: response.modelName ?? undefined,
              isStreaming: false,
              affinityStatus: response.affinityStatus ?? null,
              dataQuality: response.dataQuality ?? null,
              classRecall: response.classRecall ?? null,
              supplier360: response.supplier360 ?? null,
              supplierRisk: response.supplierRisk ?? null,
              graphTraversal: response.graphTraversal ?? null,
              agentRun: response.agentRun ?? null,
              suggestedAgent: response.suggestedAgent ?? null,
              hypotheses: response.hypotheses ?? null,
              steps: response.steps?.map(
                (s): MultiStepStep => ({
                  stepIndex: s.stepIndex,
                  description: s.description,
                  subQuestion: s.subQuestion,
                  aggregationOnly: false,
                  status: stepStatusFromResult(s.error),
                  sql: s.sql ?? null,
                  summary: s.summary ?? null,
                  error: s.error ?? null,
                  chartType: s.chartType ?? null,
                  chartOption: s.chartOption ?? null,
                  tableOption: s.tableOption ?? null,
                  visualRationale: s.visualRationale ?? null,
                })
              ),
            }),
            loading: false,
            currentAssistantMessageId: null,
          }));
          void attachHypotheses();
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
              currentAssistantMessageId: null,
              error: errMsg,
            };
          });
          throw err;
        }
      }
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : i18n.t("errors.networkError");
      const errDetail = (err as { detail?: string }).detail ?? null;
      const isInterrupted = err instanceof Error && err.message.includes(STREAM_INTERRUPTED_DETECTION);
      set((state) => {
        const last = state.messages[state.messages.length - 1];
        // 已通过 error 事件展示具体错误时，不覆盖为通用错误文案（LOW#8 修复）
        if (last?.isError) {
          return { loading: false, interrupted: isInterrupted };
        }
        return {
          messages: patchLastMessage(state.messages, {
            content: errMsg,
            errorDetail: errDetail,
            isError: true,
            isStreaming: false,
            ...(isInterrupted ? { interrupted: true } : {}),
          }),
          loading: false,
          error: errMsg,
          interrupted: isInterrupted,
        };
      });
      // P1-3：EOF 守门（stream interrupted: no done/error before EOF）需要抛出，
      // 其他错误由 inner catch 已处理，吞掉不向上传播。
      if (isInterrupted) throw err;
    } finally {
      // P1-3：loading 重置（正常/异常都需重置，避免按钮永久禁用）。
      // currentAssistantMessageId 的清理由 onDone/onError handler 负责（正常流），
      // finally 这里不再重复清（避免在 mock 测试中覆盖 handler 已设置的 null）。
      // 若 loading 仍为 true（未收到 done/error），强制复位 isStreaming（HIGH#3 兜底）。
      if (get().loading) {
        set((state) => ({
          loading: false,
          messages: patchLastMessage(state.messages, { isStreaming: false }),
        }));
      }
    }
  },

  resumeRun: async (runId, fromStepIndex, messageId) => {
    // 开跑前清掉上一次续跑/警告留下的横幅（同一条消息反复续跑时不留残影）。
    // 用定向 patch：只碰被点的那条消息，其余消息保持引用相等。
    set((state) => ({
      loading: true,
      error: null,
      messages: patchMessage(state.messages, messageId, { resumeError: null, kpiDatasourceCode: null }),
    }));
    try {
      // 事件写回被点的那条消息（不是最后一条）——见 ChatState.resumeRun 注释
      await resumeMultiStepRun(runId, fromStepIndex, streamHandlers(set, messageId));
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : i18n.t("errors.unknownError");
      // 失败**只**挂独立横幅字段，绝不写 content / isError：MessageItem 的
      // isError 分支会把整块正文（含多步计划卡与上一轮的有效回答）替换成一条错误
      // Alert —— 用户既看不到原来问出了什么，卡片连同「再续跑一次」的入口也一起消失。
      // 续跑失败是可重试的局部失败，不是这条回答的终态。
      set((state) => ({
        messages: patchMessage(state.messages, messageId, {
          resumeError: msg,
          isStreaming: false,
        }),
        loading: false,
        error: msg,
      }));
    } finally {
      // 兜底复位：续跑流异常结束（无 done/error 帧）时也要复位 loading，
      // 否则发送按钮永久禁用（同 sendMessage 的 HIGH#3）。
      // 同样认 messageId：异常路径写到最新消息也是这个 finding 的同一个洞。
      set((state) => {
        const target = state.messages.find((m) => m.id === messageId);
        if (!state.loading && !target?.isStreaming) return {};
        if (!target?.isStreaming) return { loading: false };
        return {
          messages: patchMessage(state.messages, messageId, { isStreaming: false }),
          loading: false,
        };
      });
    }
  },

  clearMessages: () => {
    invalidatePendingLoads();
    set({ messages: [], error: null });
  },

  resetSession: () => {
    const { channel } = get();
    // 「新对话」= 当前没有可恢复的目标：清指针。若照旧写入新 id，刷新会去
    // 恢复一个刚建出来的空会话（看起来像恢复失败）。
    writeLastSessionId(channel, null);
    // 在途回放不得把旧会话的消息搬回来（「新对话」之后屏幕上必须是空的）
    invalidatePendingLoads();
    set({
      messages: [],
      sessionId: makeSessionId(channel),
      loading: false,
      error: null,
    });
  },

  clearForUserSwitch: () => {
    const { channel } = get();
    // 指针由 authStore 侧 clearLastSessionIds() 清（那里才知道「换人」这件事）；
    // 这里只负责内存：messages 与 sessionId 都是上一个人的，必须换掉。
    invalidatePendingLoads();
    set({
      messages: [],
      sessionId: makeSessionId(channel),
      loading: false,
      error: null,
      sessions: [],
      sessionsError: null,
    });
  },

  sendDocQa: async (question, filters) => {
    const { sessionId, channel } = get();
    if (channel !== "doc_qa") return;
    // 与 sendMessage 同理：doc_qa 渠道自己的恢复指针也要在发送时落地
    writeLastSessionId(channel, sessionId);
    invalidatePendingLoads();

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

    try {
      await searchDocumentsQa(
        {
          sessionId,
          question,
          topK: 10,
          securityLevel: filters.securityLevel,
          documentType: filters.documentType,
        },
        (event) => {
          switch (event.kind) {
            case "meta":
              break;
            case "citations":
              set((state) => ({
                messages: patchLastMessage(state.messages, { citations: event.citations }),
              }));
              break;
            case "token":
              set((state) => {
                const last = state.messages[state.messages.length - 1];
                return {
                  messages: patchLastMessage(state.messages, {
                    content: (last.content ?? "") + event.content,
                  }),
                };
              });
              break;
            case "done":
              set((state) => ({
                messages: patchLastMessage(state.messages, {
                  tokensUsed: event.tokensUsed,
                  cost: event.cost,
                  modelName: event.modelName ?? undefined,
                  isStreaming: false,
                }),
                loading: false,
              }));
              break;
            case "error":
              set((state) => ({
                messages: patchLastMessage(state.messages, {
                  content: event.error,
                  isError: true,
                  isStreaming: false,
                }),
                loading: false,
                error: event.error,
              }));
              break;
          }
        },
      );
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : i18n.t("errors.networkError");
      set((state) => ({
        messages: patchLastMessage(state.messages, {
          content: errMsg,
          isError: true,
          isStreaming: false,
        }),
        loading: false,
        error: errMsg,
      }));
    }
  },

  // ============ 历史会话面板 actions ============

  loadSessions: async (channel) => {
    set({ sessionsLoading: true, sessionsError: null });
    try {
      const sessions = await apiListChatSessions(undefined, undefined, channel);
      set({ sessions, sessionsLoading: false });
    } catch (err) {
      const msg = err instanceof Error ? err.message : i18n.t("errors.networkError");
      set({ sessionsError: msg, sessionsLoading: false });
    }
  },

  loadSessionMessages: async (sessionId, options) => {
    const { channel } = get();
    invalidatePendingLoads();
    const seq = sessionLoadSeq;
    try {
      const resp: SessionMessagesResponse = await apiLoadSessionMessages(sessionId, undefined, {
        tail: options?.tail,
      });
      // 期间发生了更新的会话动作（切渠道 / 点了另一条历史 / 发了新消息）→ 丢弃本次结果。
      // 既不写指针也不改状态：晚到的响应盖掉用户刚做的选择，是本次改动引入的真实回归。
      if (seq !== sessionLoadSeq) return "stale";
      const messages = resp.messages.map(toChatMessage);
      writeLastSessionId(channel, sessionId);
      set({
        sessionId,
        messages,
        loading: false,
        error: null,
        sessionsError: null,
      });
      return "loaded";
    } catch (err) {
      if (seq !== sessionLoadSeq) return "stale";
      // 加载失败写入 sessionsError（用户可见于面板 Alert），不污染 chat 区 error
      // —— chat 区错误展示与历史面板错误展示语义解耦，避免互相覆盖
      const msg = err instanceof Error ? err.message : i18n.t("errors.networkError");
      // 清掉指向这个载不动的会话的指针：留着它，每次刷新都会重放同一个失败，
      // 而失败只在历史面板展开时可见（收起面板时完全静默）—— 表现为「刷新后永远空白」。
      if (readLastSessionId(channel) === sessionId) writeLastSessionId(channel, null);
      set({ sessionsError: msg });
      return "failed";
    }
  },

  deleteSession: async (sessionId) => {
    await apiDeleteSession(sessionId);
    const { channel, sessionId: currentId, sessions } = get();
    const nextSessions = sessions.filter((s) => s.sessionId !== sessionId);
    if (currentId !== sessionId) {
      // 删的是别的会话：只有当它恰好是本渠道的恢复目标时才需要清指针
      if (readLastSessionId(channel) === sessionId) writeLastSessionId(channel, null);
      set({ sessions: nextSessions });
      return;
    }
    // 删的是当前会话 → 指针必然失效（不必先问它此前有没有被写过），
    // 否则下次刷新会去恢复一个刚刚被删掉的会话
    writeLastSessionId(channel, null);
    // 在途回放不得把刚删掉的会话重新铺回屏幕
    invalidatePendingLoads();
    set({
      sessions: nextSessions,
      sessionId: makeSessionId(channel),
      messages: [],
      loading: false,
      error: null,
    });
  },

  toggleHistoryPanel: () => {
    const next = !get().historyPanelOpen;
    writeHistoryPanelOpen(next);
    set({ historyPanelOpen: next });
  },

  setHistoryPanelOpen: (open) => {
    writeHistoryPanelOpen(open);
    set({ historyPanelOpen: open });
  },
}));

// 换人即清内存会话（登录 / 登出 / 401 掉线，见 stores/userSwitch.ts）。
// 注册而非被 authStore 直接引用：避免 authStore ← chatStore ← api/client ← authStore 的循环。
onUserSwitch(() => {
  useChatStore.getState().clearForUserSwitch();
});
