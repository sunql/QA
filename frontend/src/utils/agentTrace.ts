import type { AgentTrace, AgentTraceStep } from "../types/chat";

/**
 * 系统边界收窄：agent_trace 帧 / 历史回放拿到的都是原始 JSON（不可信）。
 * 逐字段白名单校验后重建对象；任何顶层不合法都返回 null（= 不出轨迹卡）。
 *
 * 轨迹只用于展示（工具名、耗时、计数摘要），从不渲染工具结果原文 ——
 * 后端 l4_trace 已经不存原文，这里再兜一层形状校验即可。
 */

const MAX_STEPS = 50;
const VALID_STATUSES: readonly string[] = ["answered", "max_iterations"];
const STEP_STATUSES: readonly string[] = ["ok", "error"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function asNonNegativeInt(value: unknown, fallback = 0): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return fallback;
  }
  return Math.trunc(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function asStep(value: unknown): AgentTraceStep | null {
  if (!isRecord(value)) return null;
  if (typeof value.tool !== "string" || value.tool.length === 0) return null;
  if (typeof value.status !== "string" || !STEP_STATUSES.includes(value.status)) {
    return null;
  }

  const step: AgentTraceStep = {
    index: asNonNegativeInt(value.index),
    iteration: asNonNegativeInt(value.iteration),
    tool: value.tool,
    status: value.status === "error" ? "error" : "ok",
    elapsedMs: asNonNegativeInt(value.elapsedMs),
    args: asRecord(value.args),
    resultSummary: asRecord(value.resultSummary),
  };
  // error 只在失败步保留；成功步即使带脏数据也不渲染
  if (step.status === "error" && typeof value.error === "string" && value.error.trim()) {
    step.error = value.error;
  }
  return step;
}

export function asAgentTrace(value: unknown): AgentTrace | null {
  if (!isRecord(value)) return null;
  if (value.kind !== "l4_agent_trace") return null;
  if (typeof value.status !== "string" || !VALID_STATUSES.includes(value.status)) {
    return null;
  }
  if (!Array.isArray(value.steps)) return null;

  const steps = value.steps
    .slice(0, MAX_STEPS)
    .map(asStep)
    .filter((step): step is AgentTraceStep => step !== null);
  if (steps.length === 0) return null;

  const trace: AgentTrace = {
    kind: "l4_agent_trace",
    status: value.status === "max_iterations" ? "max_iterations" : "answered",
    iterations: asNonNegativeInt(value.iterations),
    totalMs: asNonNegativeInt(value.totalMs),
    stepCount: steps.length,
    steps,
  };
  if (value.truncated === true || value.steps.length > MAX_STEPS) {
    trace.truncated = true;
  }
  return trace;
}

export interface AgentTraceGroup {
  iteration: number;
  steps: AgentTraceStep[];
}

/** 毫秒 → 人类可读（<1s 用 ms，否则保留一位小数的 s）。 */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "0ms";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** 按迭代轮次分组（卡片按「第 N 轮」分节展示）；保持步骤原始顺序。 */
export function groupStepsByIteration(steps: readonly AgentTraceStep[]): AgentTraceGroup[] {
  const byIteration = new Map<number, AgentTraceStep[]>();
  for (const step of steps) {
    const group = byIteration.get(step.iteration) ?? [];
    byIteration.set(step.iteration, [...group, step]);
  }
  return [...byIteration.entries()]
    .map(([iteration, groupSteps]) => ({ iteration, steps: groupSteps }))
    .sort((a, b) => a.iteration - b.iteration);
}
