import { httpClient } from "./client";
import { API_BASE_URL } from "../config";
import type { AffinityStatus, ChatRequest, ChatResponse, ChartType, QueryPlan, SimilarQuery } from "../types/chat";
import { i18n } from "../i18n";

const BASE = "/chat";

// 与后端 ChartType 枚举对齐，供运行时校验（避免不安全 cast 把非法值透传给渲染层）
const VALID_CHART_TYPES = new Set<string>(["table", "bar", "pie", "line", "scatter"]);

function isChartType(value: unknown): value is ChartType {
  return typeof value === "string" && VALID_CHART_TYPES.has(value);
}

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
}

// step_result 事件负载（单个子步骤执行结果）
export interface StepResultView {
  stepIndex: number;
  description: string;
  subQuestion: string;
  sql?: string | null;
  data?: Record<string, unknown>[] | null;
  summary?: string | null;
  error?: string | null;
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

export function isStepResult(value: unknown): value is StepResultView {
  return isStepPlan(value);
}

export async function sendMessage(payload: ChatRequest): Promise<ChatResponse> {
  const res = await httpClient.post<ChatResponse>(BASE, payload);
  return res.data;
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

// 图表事件负载（chart 事件携带 chartType + ECharts option + 数据）
export interface StreamChartData {
  chartType: ChartType | null;
  chartOption: Record<string, unknown> | null;
  data: Record<string, unknown>[] | null;
}

// done 事件负载（累计 token / 成本 / 实际模型名 / 亲和性）
export interface StreamSummary {
  tokensUsed: number;
  cost: number;
  modelName?: string | null;
  // 会话亲和性（Phase 7）：解锁/闲聊/领域命令为 null
  affinityStatus?: AffinityStatus | null;
}

// 流式事件回调（与后端 SSE 事件一一对应）
export interface StreamEventHandlers {
  onMeta?: (intent: string) => void;
  onPlan?: (plan: QueryPlan) => void;
  onSql?: (sql: string) => void;
  onChart?: (chart: StreamChartData) => void;
  onToken?: (content: string) => void;
  onDone?: (summary: StreamSummary) => void;
  onError?: (message: string, detail?: string) => void;
  // 多步：完整计划概览 / 单个子步骤计划（进入执行）/ 单个子步骤结果
  onStepPlanOverview?: (steps: StepPlanOverviewItem[]) => void;
  onStepPlan?: (step: StepPlanView) => void;
  onStepResult?: (result: StepResultView) => void;
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
  handlers: StreamEventHandlers
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}${BASE}/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = consumeFrames(buffer, handlers);
    }
    // 冲刷解码器缓冲的尾字节：最后一个分块可能截断多字节 UTF-8 字符，
    // 流结束后必须显式解码残留字节，否则该字符被静默丢弃（HIGH#1 修复）
    buffer += decoder.decode();
    consumeFrames(buffer, handlers);
  } finally {
    reader.releaseLock();
  }
}

function consumeFrames(buffer: string, handlers: StreamEventHandlers): string {
  // 统一 CRLF / CR 为 LF：兼容 HTTP 标准换行（\r\n）与后端当前使用的 \n（HIGH#2 修复）
  const normalized = buffer.replace(/\r\n|\r/g, "\n");
  let index: number;
  let remainder = normalized;
  while ((index = remainder.indexOf("\n\n")) !== -1) {
    const frame = remainder.slice(0, index);
    remainder = remainder.slice(index + 2);
    handleFrame(frame, handlers);
  }
  return remainder;
}

function handleFrame(frame: string, handlers: StreamEventHandlers): void {
  let event = "";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    }
  }
  if (!event || dataLines.length === 0) return;

  let data: unknown;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return;
  }
  const d = data as Record<string, unknown>;

  switch (event) {
    case "meta":
      handlers.onMeta?.(typeof d.intent === "string" ? d.intent : "");
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
        chartType: isChartType(d.chartType) ? d.chartType : null,
        chartOption: (d.chartOption as Record<string, unknown>) ?? null,
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
        affinityStatus: (d.affinityStatus as AffinityStatus | null) ?? null,
      });
      break;
    case "error":
      handlers.onError?.(
        typeof d.error === "string" ? d.error : i18n.t("errors.unknownError"),
        typeof d.detail === "string" ? d.detail : undefined
      );
      break;
    case "multi_step_plan":
      if (Array.isArray(d.steps)) {
        const steps = d.steps.filter(isStepPlanOverviewItem);
        if (steps.length) {
          handlers.onStepPlanOverview?.(steps);
        }
      }
      break;
    case "step_plan":
      if (isStepPlan(d)) {
        handlers.onStepPlan?.(d);
      }
      break;
    case "step_result":
      if (isStepResult(d)) {
        handlers.onStepResult?.(d);
      }
      break;
  }
}
