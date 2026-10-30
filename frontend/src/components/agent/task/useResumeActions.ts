import { useCallback, useState } from "react";
import type { AgentRunRead, AgentRunResume } from "../../../types/agentOrchestration";
import type { BudgetIncrement } from "../../../types/agentTask";
import { buildResumePayload } from "./resumePayload";
import type { Translate } from "./errors";

/** 恢复 / 取消动作。 */
export interface ResumeActions {
  /** 恢复或取消请求进行中，用于禁用重复提交。 */
  acting: boolean;
  handleResume: () => Promise<void>;
  handleCancel: () => Promise<void>;
}

/** 恢复动作需要的上下文。 */
export interface ResumeActionsOptions {
  t: Translate;
  run: AgentRunRead | null;
  supplementaryInput: string;
  increment: BudgetIncrement | null;
  /** 恢复成功后清空表单。 */
  reset: () => void;
  resumeRun: (payload: AgentRunResume) => Promise<void>;
  cancelRun: () => Promise<void>;
  reportError: (message: string) => void;
}

/** 组装恢复载荷并触发恢复 / 取消。 */
export function useResumeActions(
  options: ResumeActionsOptions,
): ResumeActions {
  const { t, run, supplementaryInput, increment, reset } = options;
  const { resumeRun, cancelRun, reportError } = options;
  const [acting, setActing] = useState(false);

  const handleResume = useCallback(async (): Promise<void> => {
    const payload = buildResumePayload(
      t,
      run,
      supplementaryInput,
      increment,
      reportError,
    );
    if (!payload) return;
    setActing(true);
    try {
      await resumeRun(payload);
      reset();
    } finally {
      setActing(false);
    }
  }, [
    t,
    run,
    supplementaryInput,
    increment,
    reset,
    reportError,
    resumeRun,
  ]);

  const handleCancel = useCallback(async (): Promise<void> => {
    setActing(true);
    try {
      await cancelRun();
    } finally {
      setActing(false);
    }
  }, [cancelRun]);

  return { acting, handleResume, handleCancel };
}
