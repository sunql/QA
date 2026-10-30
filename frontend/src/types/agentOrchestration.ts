// Agent 编排（general agent orchestration）前后端契约类型。
// 与 backend/app/domain/agent_orchestration_schemas.py、
// backend/app/services/agent_orchestration_types.py 一一对应（JSON 为 camelCase）。

/** 任意 JSON 对象：工具入参/出参、暂停详情、工具绑定快照。 */
export type JsonObject = { [key: string]: unknown };

/** 运行状态。 */
export type RunStatus =
  | "QUEUED"
  | "RUNNING"
  | "PAUSED"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

/** 步骤类型：PLAN = Planner 决策步骤，TOOL = 工具调用步骤。 */
export type StepKind = "PLAN" | "TOOL";

/** 步骤状态。 */
export type StepStatus = "RUNNING" | "SUCCEEDED" | "FAILED" | "SKIPPED";

/** 成本来源。run 级可能出现 MIXED；单次用量行不会出现 MIXED。 */
export type CostSource = "MEASURED" | "ESTIMATED" | "UNKNOWN" | "MIXED";

/** 暂停原因。 */
export type PauseCode =
  | "NEEDS_INPUT"
  | "AMBIGUOUS"
  | "PERMISSION_DENIED"
  | "BUDGET_EXHAUSTED"
  | "TOOL_FAILED"
  | "PLANNER_OUTPUT_INVALID"
  | "AGENT_UNAVAILABLE"
  | "INFRASTRUCTURE_ERROR";

/** Planner 单步动作类型。 */
export type PlannerActionType = "CALL_TOOL" | "FINISH" | "ASK_USER";

// ---------------------------------------------------------------------------
// 配置（/agent-orchestrations）
// ---------------------------------------------------------------------------

/** POST /agent-orchestrations 请求体。 */
export interface AgentOrchestrationProfileCreate {
  agentCode: string;
  instructions: string;
  toolNames: string[];
  defaultMaxIterations: number;
  defaultMaxToolCalls: number;
  defaultMaxCostUsd: number;
  defaultTimeoutSeconds: number;
  maxObservationChars: number;
}

/** PUT /agent-orchestrations/{agentCode} 请求体（全量更新，agentCode 在路径里）。 */
export interface AgentOrchestrationProfileUpdate {
  instructions: string;
  toolNames: string[];
  defaultMaxIterations: number;
  defaultMaxToolCalls: number;
  defaultMaxCostUsd: number;
  defaultTimeoutSeconds: number;
  maxObservationChars: number;
  /** 乐观锁版本号。 */
  revision: number;
}

/** POST /agent-orchestrations/{agentCode}/toggle 请求体。 */
export interface AgentOrchestrationToggle {
  enabled: boolean;
  /** 乐观锁版本号。 */
  revision: number;
}

/** 配置里绑定的单个工具。 */
export interface AgentOrchestrationBindingRead {
  toolName: string;
  enabled: boolean;
  sortOrder: number;
}

/** 编排配置读取模型。 */
export interface AgentOrchestrationProfileRead {
  agentCode: string;
  instructions: string;
  enabled: boolean;
  defaultMaxIterations: number;
  defaultMaxToolCalls: number;
  defaultMaxCostUsd: number;
  defaultTimeoutSeconds: number;
  maxObservationChars: number;
  revision: number;
  bindings: AgentOrchestrationBindingRead[];
}

/** 可用于编排的只读工具选项（后端刻意不暴露 handlerRef）。 */
export interface AgentOrchestrationToolOptionRead {
  toolName: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// 运行（/agent-runs）
// ---------------------------------------------------------------------------

/** 单次运行的预算上限覆盖；未给出的字段回落到 profile 默认值。 */
export interface AgentRunLimits {
  maxIterations?: number;
  maxToolCalls?: number;
  maxCostUsd?: number;
  timeoutSeconds?: number;
}

/** POST /agent-runs 请求体。 */
export interface AgentRunCreate {
  agentCode: string;
  input: string;
  modelConfigId: number;
  limits?: AgentRunLimits;
}

/** POST /agent-runs/{runId}/resume 请求体（全部可选）。 */
export interface AgentRunResume {
  input?: string;
  additionalIterations?: number;
  additionalToolCalls?: number;
  additionalCostUsd?: number;
  additionalTimeoutSeconds?: number;
}

/** 列出运行的可选过滤条件；未定义的项不会被发送。 */
export interface AgentRunListFilters {
  agentCode?: string;
  status?: RunStatus;
  limit?: number;
  offset?: number;
}

/**
 * 轨迹查询的可选参数。
 *
 * `afterStepNo` 是**闭区间下界**（`step_no >= afterStepNo`）。要只取「比已持有的
 * 最后一条更新」的步骤，调用方必须传 `lastStepNo + 1`——直接传 `lastStepNo`
 * 会把最后一步重新取回，导致重复渲染。
 */
export interface AgentRunStepQuery {
  afterStepNo?: number;
  limit?: number;
}

/** POST /agent-runs 请求响应（202）。 */
export interface AgentRunCreated {
  runId: string;
  status: RunStatus;
}

/** 运行读取模型。 */
export interface AgentRunRead {
  runId: string;
  agentCode: string;
  question: string;
  createdBy: string;
  modelConfigId: number;
  status: RunStatus;
  instructionsSnapshot: string;
  toolBindingsSnapshot: JsonObject[];
  maxIterations: number;
  maxToolCalls: number;
  maxCostUsd: number;
  timeoutSeconds: number;
  iterationCount: number;
  toolCallCount: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  cost: number;
  costSource: CostSource;
  /** false 且 costSource=UNKNOWN 表示成本从未被测量，不能当作已测总量展示。 */
  costComplete: boolean;
  finalAnswer?: string | null;
  pauseCode?: PauseCode | null;
  /** pauseDetail 只有一个 budget 键，其值标明被耗尽的预算：iteration | tool | cost | time。 */
  pauseDetail?: JsonObject | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  cancelRequested: boolean;
  createdTime: string;
  updatedTime: string;
  queuedAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  revision: number;
}

/** 运行轨迹的单步。 */
export interface AgentRunStepRead {
  stepNo: number;
  iterationNo: number;
  kind: StepKind;
  status: StepStatus;
  action?: PlannerActionType | null;
  planSummary?: string | null;
  toolName?: string | null;
  inputJson?: JsonObject | null;
  outputJson?: JsonObject | null;
  answer?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  promptTokens: number;
  completionTokens: number;
  cost: number;
  startedAt: string;
  finishedAt?: string | null;
}
