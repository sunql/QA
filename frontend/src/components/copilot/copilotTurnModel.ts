/** Copilot 轮次的共享模型与纯函数（从 ProcurementCopilotPage 抽出，P4 拆分）。
 *
 * 页面 < 800 行的仓库约束 ⇒ 状态机类型与纯映射函数放这里，
 * 轨迹/线程渲染放 CopilotTurnThread，页面只保留编排状态。
 */

import type { AgentRunStepRead } from "../../types/agentOrchestration";
import type { CopilotStepPayload, CopilotTurnRead } from "../../api/procurementCopilot";

/** 运行状态 → 界面轮次状态（PAUSED 是正常终态，渲染成 DONE + pauseCode）。 */
export const RESTORED_STATUS: Record<string, CopilotTurn["status"]> = {
  SUCCEEDED: "DONE",
  PAUSED: "DONE",
  FAILED: "ERROR",
  CANCELLED: "ERROR",
};

export interface CopilotTurn {
  id: string;
  question: string;
  /** 创建时间（ms）——同条件结果复用的 24h 窗口依据 */
  createdAt?: number;
  agentCode: string;
  /** true = 对上一轮追问的回答（后端 resume 同一 run） */
  resumed?: boolean;
  runId: string | null;
  steps: CopilotStepPayload[];
  status: "RUNNING" | "DONE" | "ERROR";
  answer: string | null;
  pauseCode: string | null;
  error: string | null;
  totalTokens: number;
}

export const STATUS_COLOR: Record<string, string> = {
  RUNNING: "processing",
  DONE: "success",
  ERROR: "error",
};

/** 执行轨迹抽屉状态（`runId` 用作并发守卫：慢响应回来时不覆盖新打开的轨迹）。 */
export interface TraceState {
  runId: string | null;
  loading: boolean;
  error: string | null;
  steps: AgentRunStepRead[];
}

export const EMPTY_TRACE: TraceState = {
  runId: null,
  loading: false,
  error: null,
  steps: [],
};

export function askOf(turn: CopilotTurn): string | null {
  const ask = [...turn.steps].reverse().find((step) => step.action === "ASK_USER");
  return ask?.planSummary ?? null;
}

/** 消息 → 短标题：截到参数括号「（」前（即用户的主句），兜底 24 字。 */
export function shortTitle(question: string): string {
  const cut = question.indexOf("（");
  const base = cut > 0 ? question.slice(0, cut) : question;
  return base.length > 24 ? `${base.slice(0, 24)}…` : base;
}

/** REST 终态步骤 → 流式步骤形态：关键差异是 outputJson（observation 的
 * {text, truncated}）与**终态 status** —— 流式只推步骤到场一次（RUNNING），
 * 完成态必须回读才能渲染结构化结果卡（2026-11-23 生产实证）。 */
export function restoredStep(step: AgentRunStepRead): CopilotStepPayload {
  return {
    stepNo: step.stepNo,
    iterationNo: step.iterationNo,
    kind: step.kind,
    status: step.status,
    action: step.action ?? null,
    planSummary: step.planSummary ?? null,
    toolName: step.toolName ?? null,
    answer: step.answer ?? null,
    errorCode: step.errorCode ?? null,
    errorMessage: step.errorMessage ?? null,
    output: (step.outputJson as { text?: string } | null) ?? null,
  };
}

/** 后端恢复的轮次 → 页面轮次（无流式步骤，轨迹按 runId 现拉）。 */
export function restoredTurn(row: CopilotTurnRead, agentCode: string): CopilotTurn {
  return {
    id: `turn-${row.seq}-${row.runId}`,
    question: row.question,
    agentCode,
    resumed: false,
    runId: row.runId,
    steps: [],
    status: RESTORED_STATUS[row.status] ?? "RUNNING",
    answer: row.answer,
    pauseCode: row.pauseCode,
    error: null,
    totalTokens: 0,
  };
}
