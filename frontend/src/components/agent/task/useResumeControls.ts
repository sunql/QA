import { useCallback, useEffect, useState } from "react";
import type { AgentRunRead, AgentRunResume } from "../../../types/agentOrchestration";
import type { Translate } from "./errors";
import { budgetKeyOf } from "./resumePayload";
import type { BudgetIncrement, ResumeControls } from "../../../types/agentTask";
import { useResumeActions } from "./useResumeActions";

// 载荷组装与预算键识别已拆到 resumePayload.ts，这里保持原有导出路径不变。
export { budgetKeyOf, buildResumePayload } from "./resumePayload";

/** 恢复表单的字段状态。 */
interface ResumeFormState {
  supplementaryInput: string;
  increment: BudgetIncrement | null;
  onSupplementaryInputChange: (value: string) => void;
  onIncrementChange: (value: number | null) => void;
  reset: () => void;
}

/** 恢复表单的字段状态：补充输入与预算增量。 */
function useResumeFormState(run: AgentRunRead | null): ResumeFormState {
  const [supplementaryInput, setSupplementaryInput] = useState("");
  const [increment, setIncrement] = useState<BudgetIncrement | null>(null);
  const budgetKey = run ? budgetKeyOf(run) : null;

  // 切换到另一个运行时，旧的补充输入 / 预算增量不再适用。
  const runId = run?.runId ?? null;
  useEffect(() => {
    setSupplementaryInput("");
    setIncrement(null);
  }, [runId]);

  const reset = useCallback(() => {
    setSupplementaryInput("");
    setIncrement(null);
  }, []);

  return {
    supplementaryInput,
    increment,
    onSupplementaryInputChange: setSupplementaryInput,
    onIncrementChange: (value: number | null) =>
      setIncrement(
        value === null || !budgetKey ? null : { budget: budgetKey, value },
      ),
    reset,
  };
}

/** 恢复 / 取消的表单状态与操作。 */
export function useResumeControls(
  t: Translate,
  run: AgentRunRead | null,
  resumeRun: (payload: AgentRunResume) => Promise<void>,
  cancelRun: () => Promise<void>,
  reportError: (message: string) => void,
): ResumeControls {
  const form = useResumeFormState(run);
  const budgetKey = run ? budgetKeyOf(run) : null;
  const actions = useResumeActions({
    t,
    run,
    supplementaryInput: form.supplementaryInput,
    increment: form.increment,
    reset: form.reset,
    resumeRun,
    cancelRun,
    reportError,
  });

  const incrementValue =
    form.increment && budgetKey && form.increment.budget === budgetKey
      ? form.increment.value
      : null;

  return {
    acting: actions.acting,
    budgetKey,
    supplementaryInput: form.supplementaryInput,
    incrementValue,
    onSupplementaryInputChange: form.onSupplementaryInputChange,
    onIncrementChange: form.onIncrementChange,
    handleResume: actions.handleResume,
    handleCancel: actions.handleCancel,
  };
}
