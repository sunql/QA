// =============================================================================
// E2E mock 后端：通用 Agent 编排域（/agents、/agent-orchestrations、/agent-runs）。
// 运行状态按 agentCode 约定：demo-agent 先 PAUSED（等待补充输入），
// runner-agent 直接 RUNNING（用于验证「运行中取消」）。
// =============================================================================

import type { Route } from "@playwright/test";
import { NOW, fail, ok, respondJson, type RouteCtx } from "./mockShared";

// ===== 通用 Agent 编排相关 mock 类型 =====

/** Agent 注册表条目（mock 只保留页面用到的字段）。 */
export interface MockAgentDefinition {
  id: number;
  agentCode: string;
  agentName: string;
  description: string | null;
  triggerType: string;
  responseLatency: string;
  dataDomains: string[];
  dataLayers: string[];
  status: string;
  owner: string | null;
  version: string;
  policies: unknown[];
  runnable: boolean;
  toolName: string | null;
}

/** 编排配置里绑定的单个工具。 */
export interface MockOrchestrationBinding {
  toolName: string;
  enabled: boolean;
  sortOrder: number;
}

/** 编排配置（mock）。 */
export interface MockOrchestrationProfile {
  agentCode: string;
  instructions: string;
  enabled: boolean;
  defaultMaxIterations: number;
  defaultMaxToolCalls: number;
  defaultMaxCostUsd: number;
  defaultTimeoutSeconds: number;
  maxObservationChars: number;
  revision: number;
  bindings: MockOrchestrationBinding[];
}

/** 可绑定的只读工具选项。 */
export interface MockOrchestrationToolOption {
  toolName: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

/** 运行轨迹的单步（mock）。 */
export interface MockAgentRunStep {
  stepNo: number;
  iterationNo: number;
  kind: string;
  status: string;
  action: string | null;
  planSummary: string | null;
  toolName: string | null;
  inputJson: Record<string, unknown> | null;
  outputJson: Record<string, unknown> | null;
  answer: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  promptTokens: number;
  completionTokens: number;
  cost: number;
  startedAt: string;
  finishedAt: string | null;
}

/** 运行快照（mock；steps 只在轨迹接口里单独返回）。 */
export interface MockAgentRun {
  runId: string;
  agentCode: string;
  question: string;
  createdBy: string;
  modelConfigId: number;
  status: string;
  instructionsSnapshot: string;
  toolBindingsSnapshot: Record<string, unknown>[];
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
  costSource: string;
  costComplete: boolean;
  finalAnswer: string | null;
  pauseCode: string | null;
  pauseDetail: Record<string, unknown> | null;
  errorCode: string | null;
  errorMessage: string | null;
  cancelRequested: boolean;
  createdTime: string;
  updatedTime: string;
  queuedAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  revision: number;
  steps: MockAgentRunStep[];
}


/** 编排域状态切片。 */
export interface OrchestrationState {
  agents: MockAgentDefinition[];
  orchestrationProfiles: MockOrchestrationProfile[];
  orchestrationTools: MockOrchestrationToolOption[];
  agentRuns: MockAgentRun[];
  /** 每条运行被拉取轨迹的次数：用来模拟「同一 step_no 的行被原地改写」。 */
  stepPolls: Record<string, number>;
}

// ===== 通用 Agent 编排的种子数据 =====

// ===== 通用 Agent 编排的种子数据 =====

/** 该 Agent 的运行直接进入 RUNNING（用于验证「运行中取消」这条路径）。 */
const RUNNING_AGENT_CODE = "runner-agent";

const SEED_AGENTS: MockAgentDefinition[] = [
  {
    id: 1,
    agentCode: "demo-agent",
    agentName: "演示 Agent",
    description: "用于编排配置页的演示 Agent",
    triggerType: "user_question",
    responseLatency: "realtime",
    dataDomains: [],
    dataLayers: [],
    status: "active",
    owner: "system",
    version: "1.0.0",
    policies: [],
    runnable: true,
    toolName: "tool-alpha",
  },
  {
    id: 2,
    agentCode: RUNNING_AGENT_CODE,
    agentName: "运行中 Agent",
    description: "创建运行后直接进入 RUNNING 的 Agent",
    triggerType: "user_question",
    responseLatency: "realtime",
    dataDomains: [],
    dataLayers: [],
    status: "active",
    owner: "system",
    version: "1.0.0",
    policies: [],
    runnable: true,
    toolName: "tool-beta",
  },
  {
    id: 3,
    agentCode: "draft-agent",
    agentName: "草稿 Agent",
    description: "尚未创建编排配置的 Agent",
    triggerType: "event",
    responseLatency: "batch",
    dataDomains: [],
    dataLayers: [],
    status: "draft",
    owner: "system",
    version: "1.0.0",
    policies: [],
    runnable: false,
    toolName: null,
  },
];

const SEED_ORCHESTRATION_TOOLS: MockOrchestrationToolOption[] = [
  {
    toolName: "tool-alpha",
    description: "只读查询工具",
    inputSchema: { type: "object", properties: {} },
  },
  {
    toolName: "tool-beta",
    description: "只读统计工具",
    inputSchema: { type: "object", properties: {} },
  },
];

const SEED_ORCHESTRATION_PROFILES: MockOrchestrationProfile[] = [
  {
    agentCode: "demo-agent",
    instructions: "演示编排策略：先规划再调用只读工具。",
    enabled: true,
    defaultMaxIterations: 20,
    defaultMaxToolCalls: 30,
    defaultMaxCostUsd: 5,
    defaultTimeoutSeconds: 900,
    maxObservationChars: 50000,
    revision: 1,
    bindings: [{ toolName: "tool-alpha", enabled: true, sortOrder: 0 }],
  },
  {
    agentCode: RUNNING_AGENT_CODE,
    instructions: "运行中编排策略：直接进入执行。",
    enabled: true,
    defaultMaxIterations: 20,
    defaultMaxToolCalls: 30,
    defaultMaxCostUsd: 5,
    defaultTimeoutSeconds: 900,
    maxObservationChars: 50000,
    revision: 1,
    bindings: [],
  },
];


// ===== 通用 Agent 编排：/agents、/agent-orchestrations、/agent-runs =====

/** Agent 注册表：编排配置页用它填 Agent 下拉。 */
export async function handleAgentRegistryRoutes(route: Route, ctx: RouteCtx<OrchestrationState>): Promise<void> {
  if (ctx.path === "/agents" && ctx.method === "GET") {
    return respondJson(route, 200, ok(ctx.backend.agents));
  }
  return respondJson(route, 404, fail(`模拟后端未实现 ${ctx.method} ${ctx.path}`));
}

/** 编排配置默认值（与前端表单默认值一致）。 */
const ORCHESTRATION_DEFAULTS = {
  defaultMaxIterations: 20,
  defaultMaxToolCalls: 30,
  defaultMaxCostUsd: 5,
  defaultTimeoutSeconds: 900,
  maxObservationChars: 50000,
};

/** 把提交的工具名列表转成绑定列表（保持提交顺序作为 sortOrder）。 */
function buildBindings(toolNames: unknown): MockOrchestrationBinding[] {
  if (!Array.isArray(toolNames)) return [];
  return toolNames
    .filter((name): name is string => typeof name === "string")
    .map((toolName, index) => ({ toolName, enabled: true, sortOrder: index }));
}

/** 按提交体构造一条编排配置（新建时固定为停用）。 */
function buildProfile(
  body: Record<string, unknown>,
  enabled: boolean,
): MockOrchestrationProfile {
  const numberOr = (key: keyof typeof ORCHESTRATION_DEFAULTS, fallback: number): number => {
    const raw = body[key];
    return typeof raw === "number" ? raw : fallback;
  };
  return {
    agentCode: String(body.agentCode ?? ""),
    instructions: String(body.instructions ?? ""),
    enabled,
    defaultMaxIterations: numberOr(
      "defaultMaxIterations",
      ORCHESTRATION_DEFAULTS.defaultMaxIterations,
    ),
    defaultMaxToolCalls: numberOr(
      "defaultMaxToolCalls",
      ORCHESTRATION_DEFAULTS.defaultMaxToolCalls,
    ),
    defaultMaxCostUsd: numberOr(
      "defaultMaxCostUsd",
      ORCHESTRATION_DEFAULTS.defaultMaxCostUsd,
    ),
    defaultTimeoutSeconds: numberOr(
      "defaultTimeoutSeconds",
      ORCHESTRATION_DEFAULTS.defaultTimeoutSeconds,
    ),
    maxObservationChars: numberOr(
      "maxObservationChars",
      ORCHESTRATION_DEFAULTS.maxObservationChars,
    ),
    revision: 1,
    bindings: buildBindings(body.toolNames),
  };
}

/** 用新配置替换 backend 里的同 agentCode 配置（不可变更新）。 */
function replaceProfile(
  backend: OrchestrationState,
  updated: MockOrchestrationProfile,
): void {
  backend.orchestrationProfiles = backend.orchestrationProfiles.map((item) =>
    item.agentCode === updated.agentCode ? updated : item,
  );
}

/** 编排配置集合：工具选项 / 列表 / 新建。 */
export async function handleOrchestrationRoutes(route: Route, ctx: RouteCtx<OrchestrationState>): Promise<void> {
  const { method, path, backend } = ctx;
  if (path === "/agent-orchestrations/tool-options" && method === "GET") {
    return respondJson(route, 200, ok(backend.orchestrationTools));
  }
  if (path === "/agent-orchestrations" && method === "GET") {
    return respondJson(route, 200, ok(backend.orchestrationProfiles));
  }
  if (path === "/agent-orchestrations" && method === "POST") {
    const created = buildProfile(ctx.body, false);
    backend.orchestrationProfiles = [
      ...backend.orchestrationProfiles.filter(
        (item) => item.agentCode !== created.agentCode,
      ),
      created,
    ];
    return respondJson(route, 201, ok(created));
  }
  return handleOrchestrationItemRoutes(route, ctx);
}

/** 单条编排配置：读取 / 更新 / 启停。 */
async function handleOrchestrationItemRoutes(route: Route, ctx: RouteCtx<OrchestrationState>): Promise<void> {
  const { method, path, backend } = ctx;
  const toggleMatch = path.match(/^\/agent-orchestrations\/([^/]+)\/toggle$/);
  if (toggleMatch && method === "POST") {
    const agentCode = decodeURIComponent(toggleMatch[1]);
    const target = findProfile(backend, agentCode);
    if (!target) return respondJson(route, 404, fail("找不到对应的 Agent 或编排配置"));
    const updated: MockOrchestrationProfile = {
      ...target,
      enabled: ctx.body.enabled === true,
      revision: target.revision + 1,
    };
    replaceProfile(backend, updated);
    return respondJson(route, 200, ok(updated));
  }
  const itemMatch = path.match(/^\/agent-orchestrations\/([^/]+)$/);
  if (itemMatch && method === "GET") {
    const target = findProfile(backend, decodeURIComponent(itemMatch[1]));
    if (!target) return respondJson(route, 404, fail("找不到对应的 Agent 或编排配置"));
    return respondJson(route, 200, ok(target));
  }
  if (itemMatch && method === "PUT") {
    const agentCode = decodeURIComponent(itemMatch[1]);
    const target = findProfile(backend, agentCode);
    if (!target) return respondJson(route, 404, fail("找不到对应的 Agent 或编排配置"));
    const updated = buildProfile({ ...ctx.body, agentCode }, target.enabled);
    const withRevision = { ...updated, revision: target.revision + 1 };
    replaceProfile(backend, withRevision);
    return respondJson(route, 200, ok(withRevision));
  }
  return respondJson(route, 404, fail(`模拟后端未实现 ${method} ${path}`));
}

/** 按 agentCode 查编排配置。 */
function findProfile(
  backend: OrchestrationState,
  agentCode: string,
): MockOrchestrationProfile | undefined {
  return backend.orchestrationProfiles.find((item) => item.agentCode === agentCode);
}

/** 去掉 steps 字段：运行快照接口不返回轨迹，轨迹走 /steps 接口。 */
function publicRun(run: MockAgentRun): Omit<MockAgentRun, "steps"> {
  const { steps: _steps, ...rest } = run;
  return rest;
}

/** 构造一条 TOOL 步骤。 */
function toolStep(stepNo: number, toolName: string): MockAgentRunStep {
  return {
    stepNo,
    iterationNo: stepNo,
    kind: "TOOL",
    status: "SUCCEEDED",
    action: null,
    planSummary: null,
    toolName,
    inputJson: { supplierCode: "10105" },
    outputJson: { text: `工具 ${toolName} 返回 2 条记录` },
    answer: null,
    errorCode: null,
    errorMessage: null,
    promptTokens: 80,
    completionTokens: 20,
    cost: 0.0008,
    startedAt: NOW,
    finishedAt: NOW,
  };
}

/** 构造一条 PLAN 步骤：paused=true 时是「询问用户」的暂停步骤。 */
function planStep(paused: boolean): MockAgentRunStep {
  return {
    stepNo: 1,
    iterationNo: 1,
    kind: "PLAN",
    status: paused ? "SUCCEEDED" : "RUNNING",
    action: paused ? "ASK_USER" : "CALL_TOOL",
    planSummary: paused ? "需要补充供应商编码才能继续查询" : "正在规划查询",
    toolName: null,
    inputJson: null,
    outputJson: null,
    answer: paused ? "请提供供应商编码" : null,
    errorCode: null,
    errorMessage: null,
    promptTokens: 120,
    completionTokens: 30,
    cost: 0.0012,
    startedAt: NOW,
    finishedAt: paused ? NOW : null,
  };
}

/** 构造一条新运行：runner-agent 直接 RUNNING，其余先 PAUSED（等待用户补充输入）。 */
function buildRun(
  backend: OrchestrationState,
  body: Record<string, unknown>,
): MockAgentRun {
  const agentCode = String(body.agentCode ?? "");
  const paused = agentCode !== RUNNING_AGENT_CODE;
  return {
    runId: `run-${backend.agentRuns.length + 1}`,
    agentCode,
    question: String(body.input ?? ""),
    createdBy: "e2e",
    modelConfigId: Number(body.modelConfigId ?? 1),
    status: paused ? "PAUSED" : "RUNNING",
    instructionsSnapshot: "演示编排策略：先规划再调用只读工具。",
    toolBindingsSnapshot: [{ toolName: "tool-alpha", enabled: true, sortOrder: 0 }],
    maxIterations: 20,
    maxToolCalls: 30,
    maxCostUsd: 5,
    timeoutSeconds: 900,
    iterationCount: 1,
    toolCallCount: 0,
    promptTokens: 120,
    completionTokens: 30,
    totalTokens: 150,
    cost: 0.0012,
    costSource: "MEASURED",
    costComplete: true,
    finalAnswer: null,
    pauseCode: paused ? "NEEDS_INPUT" : null,
    pauseDetail: null,
    errorCode: null,
    errorMessage: null,
    cancelRequested: false,
    createdTime: NOW,
    updatedTime: NOW,
    queuedAt: NOW,
    startedAt: NOW,
    finishedAt: null,
    revision: 1,
    steps: [planStep(paused)],
  };
}

/** 用新快照替换 backend 里的同 runId 运行（不可变更新）。 */
function replaceRun(backend: OrchestrationState, updated: MockAgentRun): void {
  backend.agentRuns = backend.agentRuns.map((item) =>
    item.runId === updated.runId ? updated : item,
  );
}

/** 恢复运行：结束暂停，补两步工具调用并给出最终结论。 */
function resumeRun(run: MockAgentRun): MockAgentRun {
  return {
    ...run,
    status: "SUCCEEDED",
    pauseCode: null,
    pauseDetail: null,
    finalAnswer: "已完成供应商收货数量查询，共 2 条记录。",
    iterationCount: run.iterationCount + 2,
    toolCallCount: run.toolCallCount + 2,
    totalTokens: run.totalTokens + 200,
    cost: run.cost + 0.0016,
    finishedAt: NOW,
    revision: run.revision + 1,
    steps: [...run.steps, toolStep(2, "tool-alpha"), toolStep(3, "tool-alpha")],
  };
}

/** 运行集合：创建 / 列表。 */
export async function handleAgentRunRoutes(route: Route, ctx: RouteCtx<OrchestrationState>): Promise<void> {
  const { method, path, backend } = ctx;
  if (path === "/agent-runs" && method === "POST") {
    const created = buildRun(backend, ctx.body);
    backend.agentRuns = [...backend.agentRuns, created];
    return respondJson(route, 202, ok({ runId: created.runId, status: created.status }));
  }
  if (path === "/agent-runs" && method === "GET") {
    return respondJson(route, 200, ok(backend.agentRuns.map(publicRun)));
  }
  return handleAgentRunItemRoutes(route, ctx);
}

/**
 * 把一条步骤「原地改写」成终态。
 *
 * 后端的 step 行是**原地改写**的：同一个 step_no 的行从 RUNNING 被改成
 * SUCCEEDED（补上 outputJson / answer），而不是新增一行。mock 必须复现这个
 * 前提，否则前端用错游标（排他）时 E2E 仍然全绿。
 */
function settleStep(step: MockAgentRunStep): MockAgentRunStep {
  if (step.status !== "RUNNING") return step;
  return {
    ...step,
    status: "SUCCEEDED",
    finishedAt: NOW,
    outputJson:
      step.outputJson ??
      (step.kind === "TOOL"
        ? { text: `工具 ${step.toolName ?? "未知"} 返回 2 条记录` }
        : null),
    answer:
      step.answer ??
      (step.kind === "PLAN" ? "规划结论：正在查询供应商收货数量" : null),
  };
}

/** 单次运行：详情 / 轨迹 / 恢复 / 取消。 */
async function handleAgentRunItemRoutes(route: Route, ctx: RouteCtx<OrchestrationState>): Promise<void> {
  const { method, path, backend } = ctx;
  const stepsMatch = path.match(/^\/agent-runs\/([^/]+)\/steps$/);
  if (stepsMatch && method === "GET") {
    const run = findRun(backend, stepsMatch[1]);
    if (!run) return respondJson(route, 404, fail("找不到该运行"));
    const afterStepNo = Number(ctx.query.get("afterStepNo") ?? "0");
    // 第一次拉到的是原样（可能还在 RUNNING）；之后 mock 把同一 step_no 的行
    // **原地改写**成终态 —— 前端只有用闭区间下界游标（step_no >= afterStepNo）
    // 才能拿到改写后的那一行。
    const pollCount = (backend.stepPolls[run.runId] ?? 0) + 1;
    backend.stepPolls[run.runId] = pollCount;
    const steps = pollCount <= 1 ? run.steps : run.steps.map(settleStep);
    return respondJson(
      route,
      200,
      ok(steps.filter((step) => step.stepNo >= afterStepNo)),
    );
  }
  const resumeMatch = path.match(/^\/agent-runs\/([^/]+)\/resume$/);
  if (resumeMatch && method === "POST") {
    const run = findRun(backend, resumeMatch[1]);
    if (!run) return respondJson(route, 404, fail("找不到该运行"));
    const updated = resumeRun(run);
    replaceRun(backend, updated);
    return respondJson(route, 200, ok(publicRun(updated)));
  }
  const cancelMatch = path.match(/^\/agent-runs\/([^/]+)\/cancel$/);
  if (cancelMatch && method === "POST") {
    const run = findRun(backend, cancelMatch[1]);
    if (!run) return respondJson(route, 404, fail("找不到该运行"));
    const updated: MockAgentRun = {
      ...run,
      status: "CANCELLED",
      cancelRequested: true,
      finishedAt: NOW,
      revision: run.revision + 1,
    };
    replaceRun(backend, updated);
    return respondJson(route, 200, ok(publicRun(updated)));
  }
  const detailMatch = path.match(/^\/agent-runs\/([^/]+)$/);
  if (detailMatch && method === "GET") {
    const run = findRun(backend, detailMatch[1]);
    if (!run) return respondJson(route, 404, fail("找不到该运行"));
    return respondJson(route, 200, ok(publicRun(run)));
  }
  return respondJson(route, 404, fail(`模拟后端未实现 ${method} ${path}`));
}

/** 按 runId 查运行（runId 来自 URL，可能已被编码）。 */
function findRun(backend: OrchestrationState, rawRunId: string): MockAgentRun | undefined {
  const runId = decodeURIComponent(rawRunId);
  return backend.agentRuns.find((item) => item.runId === runId);
}


/** 编排域种子（深拷贝）。 */
export function orchestrationSeed(): OrchestrationState {
  return {
    agents: SEED_AGENTS.map((a) => ({ ...a })),
    orchestrationProfiles: SEED_ORCHESTRATION_PROFILES.map((p) => ({
      ...p,
      bindings: p.bindings.map((b) => ({ ...b })),
    })),
    orchestrationTools: SEED_ORCHESTRATION_TOOLS.map((t) => ({ ...t })),
    agentRuns: [],
    stepPolls: {},
  };
}
