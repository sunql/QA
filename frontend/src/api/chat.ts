import { httpClient } from "./client";
import { API_BASE_URL } from "../config";
import type { AffinityStatus, ChatRequest, ChatResponse, ChartType, ClassRecallInfo, DataQualityBadge, HypothesisView, QueryPlan, SimilarQuery, TablePayload, VisualRationale } from "../types/chat";
import { i18n } from "../i18n";
import { authHeaders } from "./authHeaders";
import { asChartOption, asTablePayload, asVisualRationale, normalizeChartType } from "../utils/chartContract";
import { asReportSections } from "../utils/reportSections";
import { StreamChunkParser } from "../stores/stream_chunk_parser";

const BASE = "/chat";

// ReAct 查询计划运行时校验（M2）：API 为系统边界，形状不符时不渲染 QueryPlanCard
function isQueryPlan(value: unknown): value is QueryPlan {
  if (!value || typeof value !== "object") return false;
  const p = value as Record<string, unknown>;
  return (
    typeof p.target === "string" &&
    Array.isArray(p.selectedClasses) &&
    Array.isArray(p.selectedProperties) &&
    Array.isArray(p.conditions) &&
    Array.isArray(p.aggregations) &&
    Array.isArray(p.groupBy) &&
    Array.isArray(p.joins) &&
    Array.isArray(p.sortBy) &&
    (typeof p.rowLimit === "number" || p.rowLimit === null)
  );
}

// ===== 多步查询流式事件负载与运行时校验 =====

// step_plan 事件负载（单个子步骤计划，含汇总步骤）
export interface StepPlanView {
  stepIndex: number;
  description: string;
  subQuestion: string;
}

// multi_step_plan 事件负载（完整计划概览，循环前一次下发）
export interface StepPlanOverviewItem extends StepPlanView {
  aggregationOnly: boolean;
  // F7/IMP-6：续跑时后端为「已跳过（已持久化完成）」的步回放终态。只认 "done"
  // （唯一被回放的终态），缺省 ⇒ 前端按「待执行」处理。
  status?: "done";
}

// step_result 事件负载（单个子步骤执行结果）
// 图表两字段由决策引擎每步各自产出（失败步骤为 null）——收窄后恒存在。
export interface StepResultView {
  stepIndex: number;
  description: string;
  subQuestion: string;
  sql?: string | null;
  data?: Record<string, unknown>[] | null;
  summary?: string | null;
  error?: string | null;
  chartType?: ChartType | null;
  chartOption?: Record<string, unknown> | null;
  // 每步的明细表负载 + 判断依据（0107）；失败步骤为 null
  tableOption?: TablePayload | null;
  visualRationale?: VisualRationale | null;
  queryPlan?: QueryPlan | null;
}

// step_compressed 事件负载（Task 6 新增）：某个更早的步被压缩后补发
export interface StepCompressedView {
  stepIndex: number;
  originalRows: number;
  compressedRows: number;
}

function isStepIndex(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

// 运行时收窄：stepIndex/description/subQuestion 必须为合法值，非法负载不渲染
export function isStepPlan(value: unknown): value is StepPlanView {
  if (!value || typeof value !== "object") return false;
  const s = value as Record<string, unknown>;
  return (
    isStepIndex(s.stepIndex) &&
    typeof s.description === "string" &&
    typeof s.subQuestion === "string"
  );
}

function isStepPlanOverviewItem(value: unknown): value is StepPlanOverviewItem {
  const record = value as Record<string, unknown>;
  return isStepPlan(value) && typeof record.aggregationOnly === "boolean";
}

/**
 * F7/IMP-6：收窄概览步的回放终态（系统边界）。
 *
 * 后端续跑时会给「已跳过」的步回放 `status: "done"`；旧后端 / 异常值不带。这里只认
 * `"done"`，其余（未知字符串、数字、null）一律归零为 undefined ⇒ 前端按「待执行」处理，
 * 不让未校验的值流进 store。返回**新对象**（不可变），不原地改 SSE 帧。
 */
function normalizeOverviewStatus(item: StepPlanOverviewItem): StepPlanOverviewItem {
  const raw = (item as { status?: unknown }).status;
  return raw === "done" ? { ...item, status: "done" } : { ...item, status: undefined };
}

export function isStepResult(value: unknown): value is StepResultView {
  return isStepPlan(value);
}

/**
 * 收窄 step_result 负载（系统边界）：形状不符返回 null，图表字段非法一律置 null。
 *
 * 返回**新对象**（不可变），不原地改 SSE 帧。图表字段是决策引擎多步每步出图的
 * 载体：失败步骤不带这两字段，收窄后为 null，渲染层据此不画（而不是画一张空图）。
 */
function normalizeStepResult(value: unknown): StepResultView | null {
  if (!isStepResult(value)) return null;
  const record = value as unknown as Record<string, unknown>;
  return {
    ...value,
    chartType: normalizeChartType(record.chartType),
    chartOption: asChartOption(record.chartOption),
    tableOption: asTablePayload(record.tableOption),
    visualRationale: asVisualRationale(record.visualRationale),
  };
}

/** 收窄非流式响应（同一道系统边界：白名单只对 SSE 帧生效会让两条路径口径分叉）。 */
function normalizeChatResponse(response: ChatResponse): ChatResponse {
  const record = response as unknown as Record<string, unknown>;
  return {
    ...response,
    chartType: normalizeChartType(record.chartType),
    chartOption: asChartOption(record.chartOption),
    tableOption: asTablePayload(record.tableOption),
    visualRationale: asVisualRationale(record.visualRationale),
    steps: Array.isArray(response.steps)
      ? response.steps
          .map(normalizeStepResult)
          .filter((step): step is StepResultView => step !== null)
      : response.steps,
  };
}

export async function sendMessage(payload: ChatRequest): Promise<ChatResponse> {
  const res = await httpClient.post<ChatResponse>(BASE, payload);
  return normalizeChatResponse(res.data);
}

// ===== v3.1 B6（M7 Hypothesis Hook）：「可能原因」假设 =====

// 后端 HYPOTHESIS_MAX_COUNT 对齐：前端再裁一次防御
export const HYPOTHESIS_MAX_COUNT = 3;

// 运行时收窄（系统边界）：形状不符的条目直接丢弃，不渲染
export function isHypothesis(value: unknown): value is HypothesisView {
  if (!value || typeof value !== "object") return false;
  const h = value as Record<string, unknown>;
  return (
    typeof h.id === "number" &&
    typeof h.statement === "string" &&
    h.statement.length > 0 &&
    typeof h.verificationSql === "string" &&
    h.verificationSql.length > 0 &&
    (h.driver === null || h.driver === undefined || typeof h.driver === "string") &&
    (h.turnQuestion === null || h.turnQuestion === undefined || typeof h.turnQuestion === "string") &&
    (h.createdTime === null || h.createdTime === undefined || typeof h.createdTime === "string")
  );
}

/**
 * 拉取某会话最新分析假设（GET /chat/sessions/{sessionId}/hypotheses）。
 *
 * 流式路径假设不进 SSE 帧，前端在答案流结束后调用；非法条目过滤 +
 * 上限裁剪（HYPOTHESIS_MAX_COUNT）后返回，失败/为空返回空数组。
 */
export async function fetchHypotheses(
  sessionId: string,
  limit = 10
): Promise<HypothesisView[]> {
  const res = await httpClient.get<HypothesisView[]>(
    `${BASE}/sessions/${encodeURIComponent(sessionId)}/hypotheses`,
    { params: { limit } }
  );
  const list = Array.isArray(res.data) ? res.data : [];
  return list.filter(isHypothesis).slice(0, HYPOTHESIS_MAX_COUNT);
}

// 相似历史问法（输入联想）：调用 POST /chat/suggest
export async function getSuggestions(
  question: string,
  datasourceId?: number | null
): Promise<SimilarQuery[]> {
  const res = await httpClient.post<{ suggestions: SimilarQuery[] }>(`${BASE}/suggest`, {
    question,
    datasourceId: datasourceId ?? null,
  });
  return res.data.suggestions;
}

// ===== SSE 流式（5.6）=====

// 图表事件负载（chart 事件携带 chartType + ECharts option + 数据 + 明细表 + 判断依据）
export interface StreamChartData {
  chartType: ChartType | null;
  chartOption: Record<string, unknown> | null;
  tableOption: TablePayload | null;
  visualRationale: VisualRationale | null;
  data: Record<string, unknown>[] | null;
}

// done 事件负载（累计 token / 成本 / 实际模型名 / 亲和性 / 拦截类卡片对象）
export interface StreamSummary {
  tokensUsed: number;
  cost: number;
  modelName?: string | null;
  // 本轮端到端耗时（毫秒，后端 done 帧 `latency_ms`）：计划卡摘要的耗时来源。
  // 历史回放没有该字段 ⇒ null（不伪造）。
  latencyMs?: number | null;
  // 会话亲和性（Phase 7）：解锁/闲聊/领域命令为 null
  affinityStatus?: AffinityStatus | null;
  // #207 审查 HIGH 修复：流式 done 事件携带拦截类卡片对象（前端按存在性回填渲染）
  agentRun?: import("../types/agentRuntime").AgentRunRead | null;
  supplier360?: import("../types/supplier").Supplier360Read | null;
  supplierRisk?: import("../types/supplierRisk").SupplierRiskRead | null;
  graphTraversal?: import("../types/graphTraversal").GraphTraversalRead | null;
  // Phase 7 G4：未指名 Agent 语义路由建议卡片（中置信命中时随 done 帧透传）
  suggestedAgent?: import("../types/chat").AgentSuggestion | null;
  // 多步时顶层查询计划
  queryPlan?: import("../types/chat").QueryPlan | null;
  // 0107：多步汇总/降级收尾的判断依据（SUMMARY_TEXT_ONLY）。done 帧**不带 tableOption**
  //（多步顶层无表；单步表走 chart 事件）。单步 done 帧也不带此字段（其依据已由 chart 事件下发）。
  visualRationale?: import("../types/chat").VisualRationale | null;
  // 2026-10-31：多步报告分块（done 帧）；
  // 注意：本批**不**顺带补 done 帧的 queryPlan 缺口（既有行为，改动会影响卡片出现时机）。
  reportSections?: import("../types/chat").ReportBlock[] | null;
}

// data_quality 事件负载（Phase 1.4）：每张 selectedClass 对应一条 badge
export interface StreamDataQualityPayload {
  badges: DataQualityBadge[];
}

// 类召回诊断运行时校验（系统边界）：形状不符时不触发回调
function isClassRecallInfo(value: unknown): value is ClassRecallInfo {
  if (!value || typeof value !== "object") return false;
  const r = value as Record<string, unknown>;
  return (
    (r.mode === "recall" || r.mode === "expanded" || r.mode === "fallback") &&
    typeof r.hitCount === "number" &&
    typeof r.classCount === "number" &&
    typeof r.truncated === "boolean"
  );
}

// 流式事件回调（与后端 SSE 事件一一对应）
export interface StreamEventHandlers {
  // F2：KPI 数据源不一致警告（code + warning）
  onMetaWarning?: (code: string, warning: string) => void;
  onMeta?: (intent: string) => void;
  onPlan?: (plan: QueryPlan) => void;
  onSql?: (sql: string) => void;
  onChart?: (chart: StreamChartData) => void;
  onToken?: (content: string) => void;
  onDone?: (summary: StreamSummary) => void;
  // errorType（P2-1）：后端细分错误类型（permission / provider_transient / ...），
  // 供 UI 差异化降级；系统边界原始字符串，渲染前必须过白名单。
  onError?: (message: string, detail?: string, errorType?: string) => void;
  // 多步：完整计划概览 / 单个子步骤计划（进入执行）/ 单个子步骤结果
  // runId：Task 6 起 multi_step_plan 事件携带；单步路径不发 ⇒ undefined
  onStepPlanOverview?: (steps: StepPlanOverviewItem[], runId?: string) => void;
  onStepPlan?: (step: StepPlanView) => void;
  onStepResult?: (result: StepResultView) => void;
  // 多步：某个**更早**的步被上下文压缩（其 step_result 早已发过，故单独补一条）
  onStepCompressed?: (payload: StepCompressedView) => void;
  // Phase 1.4：目标表可信度 badge
  onDataQuality?: (payload: StreamDataQualityPayload) => void;
  // 类召回诊断（2026-09-16）：截断/降级时前端提示
  onClassRecall?: (info: ClassRecallInfo) => void;
}

/**
 * 通过 POST /chat/stream 发起 SSE 流式对话。
 *
 * 使用原生 fetch + ReadableStream（axios 不支持流式读取），逐帧解析
 * `event: X\ndata: {json}\n\n`，按事件类型分发到回调。网络错误抛 Error，
 * 由调用方（store）转为错误消息。
 */
export async function sendMessageStream(
  payload: ChatRequest,
  handlers: StreamEventHandlers,
  options?: { lastEventId?: string }
): Promise<void> {
  const headers = authHeaders({ "Content-Type": "application/json" });
  if (options?.lastEventId) headers["Last-Event-ID"] = options.lastEventId;
  const response = await fetch(`${API_BASE_URL}${BASE}/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`请求失败 (HTTP ${response.status})`);
  }
  if (!response.body) {
    throw new Error(i18n.t("errors.noStreamSupport"));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new StreamChunkParser();
  let sawTerminal = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const text = decoder.decode(value, { stream: true });
      for (const frame of parser.feed(text)) {
        if (frame.event === "done" || frame.event === "error") sawTerminal = true;
        dispatchFrame(frame, handlers);
      }
    }
    const tail = decoder.decode();
    if (tail) {
      for (const frame of parser.feed(tail)) {
        if (frame.event === "done" || frame.event === "error") sawTerminal = true;
        dispatchFrame(frame, handlers);
      }
    }
    for (const frame of parser.flush()) {
      if (frame.event === "done" || frame.event === "error") sawTerminal = true;
      dispatchFrame(frame, handlers);
    }
    if (!sawTerminal) throw new Error("stream interrupted: no done/error before EOF");
  } finally {
    reader.releaseLock();
  }
}

function dispatchFrame(frame: { event: string; data: string }, handlers: StreamEventHandlers): void {
  const { event, data } = frame;
  if (!event) return;

  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return;
  }
  const d = parsed as Record<string, unknown>;

  switch (event) {
    case "meta":
      handlers.onMeta?.(typeof d.intent === "string" ? d.intent : "");
      if (d.datasourceMismatch === true && typeof d.code === "string" && typeof d.warning === "string") {
        handlers.onMetaWarning?.(d.code, d.warning);
      }
      break;
    case "plan":
      if (isQueryPlan(d.plan)) {
        handlers.onPlan?.(d.plan);
      }
      break;
    case "sql":
      handlers.onSql?.(typeof d.sql === "string" ? d.sql : "");
      break;
    case "chart":
      handlers.onChart?.({
        chartType: normalizeChartType(d.chartType),
        chartOption: asChartOption(d.chartOption),
        tableOption: asTablePayload(d.tableOption),
        visualRationale: asVisualRationale(d.visualRationale),
        data: (d.data as Record<string, unknown>[]) ?? null,
      });
      break;
    case "token":
      if (typeof d.content === "string") {
        handlers.onToken?.(d.content);
      }
      break;
    case "done":
      handlers.onDone?.({
        tokensUsed: typeof d.tokensUsed === "number" ? d.tokensUsed : 0,
        cost: typeof d.cost === "number" ? d.cost : 0,
        modelName: typeof d.modelName === "string" ? d.modelName : null,
        latencyMs: typeof d.latency_ms === "number" ? d.latency_ms : null,
        affinityStatus: (d.affinityStatus as AffinityStatus | null) ?? null,
        agentRun: (d.agentRun as StreamSummary["agentRun"]) ?? null,
        supplier360: (d.supplier360 as StreamSummary["supplier360"]) ?? null,
        supplierRisk: (d.supplierRisk as StreamSummary["supplierRisk"]) ?? null,
        graphTraversal: (d.graphTraversal as StreamSummary["graphTraversal"]) ?? null,
        suggestedAgent: (d.suggestedAgent as StreamSummary["suggestedAgent"]) ?? null,
        visualRationale: asVisualRationale(d.visualRationale),
        reportSections: asReportSections(d.reportSections),
      });
      break;
    case "error":
      handlers.onError?.(
        typeof d.error === "string" ? d.error : i18n.t("errors.unknownError"),
        typeof d.detail === "string" ? d.detail : undefined,
        typeof d.errorType === "string" ? d.errorType : undefined
      );
      break;
    case "multi_step_plan":
      if (Array.isArray(d.steps)) {
        const steps = d.steps.filter(isStepPlanOverviewItem).map(normalizeOverviewStatus);
        if (steps.length) {
          // 单步路径不发 runId ⇒ undefined（前端据此不渲染续跑按钮）
          handlers.onStepPlanOverview?.(
            steps,
            typeof d.runId === "string" ? d.runId : undefined
          );
        }
      }
      break;
    case "step_compressed":
      if (isStepIndex(d.stepIndex)) {
        handlers.onStepCompressed?.({
          stepIndex: d.stepIndex,
          originalRows: typeof d.originalRows === "number" ? d.originalRows : 0,
          compressedRows: typeof d.compressedRows === "number" ? d.compressedRows : 0,
        });
      }
      break;
    case "step_plan":
      if (isStepPlan(d)) {
        handlers.onStepPlan?.(d);
      }
      break;
    case "step_result": {
      const stepResult = normalizeStepResult(d);
      if (stepResult) {
        handlers.onStepResult?.(stepResult);
      }
      break;
    }
    case "data_quality":
      if (Array.isArray(d.badges)) {
        const badges = d.badges.filter(isDataQualityBadge);
        if (badges.length) {
          handlers.onDataQuality?.({ badges });
        }
      }
      break;
    case "class_recall":
      if (isClassRecallInfo(d)) {
        handlers.onClassRecall?.(d);
      }
      break;
  }
}

function isDataQualityBadge(value: unknown): value is DataQualityBadge {
  if (!value || typeof value !== "object") return false;
  const b = value as Record<string, unknown>;
  return (
    typeof b.targetTable === "string" &&
    typeof b.evaluated === "boolean" &&
    (b.overallScore === null || typeof b.overallScore === "string") &&
    (b.evaluatedAt === null || typeof b.evaluatedAt === "string") &&
    (b.rulesCount === null || typeof b.rulesCount === "number")
  );
}

// ===== 多步续跑（spec §N4）=====

export interface ResumeMultiStepOptions {
  /** 续跑使用的模型 id；缺省 = 沿用 run 原 model。 */
  modelId?: number | null;
}

/**
 * 续跑失败的 multi-step run。
 *
 * - runId: MultiStepRun.id（UUID 字符串）
 * - fromStepIndex: 从该 step 起重跑；后端自动定位首个 failed/skipped step
 * - handlers: SSE 事件回调（与 sendMessageStream 同款）
 * - options.modelId: 续跑使用的模型 id；null/undefined = 沿用 run 原 model
 *
 * 后端路由：POST /api/v1/chat/multi-step/{runId}/resume
 * 字段名走 camelCase（与后端 CamelModel alias 对齐）。
 */
export async function resumeMultiStepRun(
  runId: string,
  fromStepIndex: number,
  handlers: StreamEventHandlers,
  options?: ResumeMultiStepOptions,
): Promise<void> {
  const headers = authHeaders({ "Content-Type": "application/json" });
  headers["Idempotency-Key"] = `resume-${runId}-${Date.now()}`;
  const body: Record<string, unknown> = { fromStepIndex };
  if (options && options.modelId !== undefined) {
    body.modelOverride = options.modelId;
  }
  const response = await fetch(
    `${API_BASE_URL}${BASE}/multi-step/${encodeURIComponent(runId)}/resume`,
    {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    }
  );

  if (!response.ok) {
    throw new Error(`续跑失败 (HTTP ${response.status})`);
  }
  if (!response.body) {
    throw new Error(i18n.t("errors.noStreamSupport"));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new StreamChunkParser();
  let sawTerminal = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const text = decoder.decode(value, { stream: true });
      for (const frame of parser.feed(text)) {
        if (frame.event === "done" || frame.event === "error") sawTerminal = true;
        dispatchFrame(frame, handlers);
      }
    }
    const tail = decoder.decode();
    if (tail) {
      for (const frame of parser.feed(tail)) {
        if (frame.event === "done" || frame.event === "error") sawTerminal = true;
        dispatchFrame(frame, handlers);
      }
    }
    for (const frame of parser.flush()) {
      if (frame.event === "done" || frame.event === "error") sawTerminal = true;
      dispatchFrame(frame, handlers);
    }
    if (!sawTerminal) throw new Error("stream interrupted: no done/error before EOF");
  } finally {
    reader.releaseLock();
  }
}
