import type { AgentRunRead, AgentRunResume } from "../../../types/agentOrchestration";
import type { BudgetIncrement, BudgetKey } from "../../../types/agentTask";
import type { Translate } from "./errors";

/** pauseDetail.budget 的合法取值白名单。 */
const BUDGET_KEYS: readonly BudgetKey[] = ["iteration", "tool", "cost", "time"];

/** pauseDetail 是单个 budget 键，值为四种预算名之一。 */
export function budgetKeyOf(run: AgentRunRead): BudgetKey | null {
  const raw = run.pauseDetail?.budget;
  return BUDGET_KEYS.includes(raw as BudgetKey) ? (raw as BudgetKey) : null;
}

/** 把预算增量写入 resume 载荷，字段名与后端 _BUDGET_INCREMENT_BY_BUDGET 一致。 */
function applyIncrement(
  payload: AgentRunResume,
  budget: BudgetKey,
  value: number,
): void {
  if (budget === "iteration") payload.additionalIterations = value;
  else if (budget === "tool") payload.additionalToolCalls = value;
  else if (budget === "cost") payload.additionalCostUsd = value;
  else payload.additionalTimeoutSeconds = value;
}

/**
 * 组装恢复载荷：只有后端明确要求的字段才会被提交。
 *
 * 补充输入对应 NEEDS_INPUT / AMBIGUOUS；预算增量对应 BUDGET_EXHAUSTED，
 * 字段名由 pauseDetail.budget 决定。缺字段时先报错并返回 null，不发起请求。
 */
export function buildResumePayload(
  t: Translate,
  run: AgentRunRead | null,
  supplementaryInput: string,
  increment: BudgetIncrement | null,
  reportError: (message: string) => void,
): AgentRunResume | null {
  if (!run) return null;
  const pauseCode = run.pauseCode ?? null;
  const currentBudget = budgetKeyOf(run);
  const payload: AgentRunResume = {};

  if (pauseCode === "NEEDS_INPUT" || pauseCode === "AMBIGUOUS") {
    const trimmed = supplementaryInput.trim();
    if (!trimmed) {
      reportError(t("agentTask.messages.resumeInputRequired"));
      return null;
    }
    payload.input = trimmed;
  }
  if (pauseCode === "BUDGET_EXHAUSTED") {
    if (!currentBudget) {
      reportError(t("agentTask.messages.budgetDetailMissing"));
      return null;
    }
    if (!increment || increment.budget !== currentBudget) {
      reportError(t("agentTask.messages.resumeIncrementRequired"));
      return null;
    }
    applyIncrement(payload, currentBudget, increment.value);
  }
  return payload;
}
