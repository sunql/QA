// 对话 / NL2SQL / 图表渲染相关类型（对齐后端 ChatRequest / ChatResponse）

// 多轮对话意图：query/new_query 全新查询，refine/follow_up 需上一轮查询状态，
// clarify 概念解释（不进 NL2SQL），chitchat 闲聊；define/map/metric 为本体治理指令
// （定义指标 / 映射属性 / 列举指标，Phase 2 接入）
export type IntentType =
  | "query"
  | "chitchat"
  | "refine"
  | "follow_up"
  | "new_query"
  | "clarify"
  | "define"
  | "map"
  | "metric";

export type ChartType = "table" | "bar" | "pie" | "line" | "scatter";

// 后端从问题中抽取的结构化查询实体（best-effort，任一字段可为空）
export interface ExtractedEntities {
  dimension?: string | null;
  metric?: string | null;
  chartType?: ChartType | null;
}

// ReAct 查询计划（对齐后端 QueryPlan.to_dict()）
export interface AggregationSpec {
  function: string;
  property: string;
  alias?: string | null;
  // 派生指标表达式（如占比：SUM(数量) / SUM(SUM(数量)) OVER ()）；存在时优先展示
  formula?: string | null;
}

export interface JoinSpec {
  sourceClass: string;
  targetClass: string;
  columns: string[];
}

export interface SortSpec {
  property: string;
  direction: "asc" | "desc";
}

export interface QueryPlan {
  target: string;
  selectedClasses: string[];
  selectedProperties: string[];
  conditions: string[];
  aggregations: AggregationSpec[];
  groupBy: string[];
  joins: JoinSpec[];
  sortBy: SortSpec[];
  rowLimit: number | null;
  // LLM 对问题的一句话理解（含术语→本体概念映射）；缺省为空
  interpretation?: string | null;
}

// 历史消息（客户端回传最近若干轮）
export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
}

// 相似历史问法（对齐后端 SimilarQuery，输入联想用）
export interface SimilarQuery {
  question: string;
  sql?: string | null;
  similarity: number;
}

// 发送消息请求
export interface ChatRequest {
  sessionId: string;
  question: string;
  datasourceId: number;
  history: HistoryMessage[];
  // 指定使用的模型 ID；undefined/null 表示自动路由
  modelId?: number | null;
  // 用户显式指定的图表类型；undefined/null 表示由系统按数据形状自动推荐
  chartType?: ChartType | null;
}

// 多步查询单个子步骤结果（对齐后端 StepResultRead，非流式响应 steps 数组项）
export interface StepResultRead {
  stepIndex: number;
  description: string;
  subQuestion: string;
  sql?: string | null;
  data?: Record<string, unknown>[] | null;
  summary?: string | null;
  error?: string | null;
}

// 多步子步骤的运行时状态（前端聚合，随流式事件推进）
export type StepStatus = "pending" | "running" | "done" | "error";

// 单个多步子步骤（含前端维护的状态与回填结果）
export interface MultiStepStep {
  stepIndex: number;
  description: string;
  subQuestion: string;
  // 汇总步骤不执行 SQL，仅产生最终对比结论
  aggregationOnly: boolean;
  status: StepStatus;
  sql?: string | null;
  summary?: string | null;
  error?: string | null;
}

// 后端对话响应（chartOption 为 ECharts option JSON）
export interface ChatResponse {
  answer: string;
  intent: IntentType;
  sql?: string | null;
  chartType?: ChartType | null;
  chartOption?: Record<string, unknown> | null;
  data?: Record<string, unknown>[] | null;
  // ReAct 第一阶段生成并校验通过的查询计划（前端可折叠展示）
  queryPlan?: QueryPlan | null;
  tokensUsed: number;
  cost: number;
  // 实际服务于本次回答的大模型名称（闲聊/领域命令为 null）
  modelName?: string | null;
  // 从问题中抽取的查询实体（仅查询意图返回，其余为 null）
  extractedEntities?: ExtractedEntities | null;
  // 会话亲和性（Phase 7）：解锁/闲聊/领域命令时为 null
  affinityStatus?: AffinityStatus | null;
  // 多步查询各子步骤结果（仅 intent=multi_step 时填充；单步查询为 null）
  steps?: StepResultRead[] | null;
}

// 会话亲和性状态：前 N 轮锁定模型 + 剩余轮数（解锁时为 null）
export interface AffinityStatus {
  lockedModel: string;
  remainingTurns: number;
}

// 前端消息（后端响应 + UI 状态）
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: number;
  sql?: string | null;
  chartType?: ChartType | null;
  chartOption?: Record<string, unknown> | null;
  data?: Record<string, unknown>[] | null;
  // ReAct 查询计划（非流式响应回填；流式期间隐藏展示）
  queryPlan?: QueryPlan | null;
  tokensUsed?: number;
  cost?: number;
  // 实际服务于本次回答的大模型名称（done 事件回填）
  modelName?: string;
  isError?: boolean;
  // 意图（meta 事件回填）
  intent?: IntentType;
  // 从问题中抽取的查询实体（仅查询意图回填）
  extractedEntities?: ExtractedEntities | null;
  // 流式输出中：占位消息正在接收 token（UI 显示打字光标）
  isStreaming?: boolean;
  // NL2SQL 校验失败的具体差异（error 事件透传 detail）
  errorDetail?: string | null;
  // 会话亲和性状态（done 事件回填；解锁时为 null/undefined）
  affinityStatus?: AffinityStatus | null;
  // 多步子步骤列表（multi_step_plan 事件建立，随 step_plan/step_result 推进状态）
  steps?: MultiStepStep[];
  // 当前正在执行的步骤序号（step_plan 事件回填）
  currentStepIndex?: number;
  // 后端 SessionMessage 主键（PDF 单条导出需要：chatStore 暂未在 sendMessage
  // 完成后回填，故默认 undefined，全局按钮正常工作，单条入口 disabled）
  dbMessageId?: number;
}
