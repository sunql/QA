import type { ModelConfig } from "./modelConfig";
import type {
  AgentOrchestrationProfileRead,
  AgentRunRead,
  AgentRunResume,
  AgentRunStepRead,
} from "./agentOrchestration";

/** pauseDetail.budget 的取值：说明是哪一项预算被耗尽。 */
export type BudgetKey = "iteration" | "tool" | "cost" | "time";

/** 预算增量：budget 指明被耗尽的预算项，value 是要追加的数量。 */
export interface BudgetIncrement {
  budget: BudgetKey;
  value: number;
}

/** 创建运行表单的字段、提交动作与可选项。 */
export interface CreateRunForm {
  /** 启用中的编排配置。 */
  profiles: AgentOrchestrationProfileRead[];
  /** 启用中的模型。 */
  models: ModelConfig[];
  agentCode: string | undefined;
  modelConfigId: number | undefined;
  input: string;
  /** 创建请求进行中，用于禁用重复提交。 */
  creating: boolean;
  /** 可选项加载失败时的提示；非空时页面顶部展示错误条。 */
  loadError: string | null;
  onAgentCodeChange: (value: string) => void;
  onModelChange: (value: number) => void;
  onInputChange: (value: string) => void;
  handleCreate: () => Promise<void>;
}

/** 恢复 / 取消相关的表单状态与操作。 */
export interface ResumeControls {
  /** 恢复或取消请求进行中，用于禁用重复提交。 */
  acting: boolean;
  /** 与 pauseDetail.budget 匹配的预算项；缺失时为 null。 */
  budgetKey: BudgetKey | null;
  supplementaryInput: string;
  /** 当前输入的增量值；预算项不匹配时为 null。 */
  incrementValue: number | null;
  onSupplementaryInputChange: (value: string) => void;
  onIncrementChange: (value: number | null) => void;
  handleResume: () => Promise<void>;
  handleCancel: () => Promise<void>;
}

/** 一次运行的跟踪状态与操作。 */
export interface AgentRunTracker {
  run: AgentRunRead | null;
  steps: AgentRunStepRead[];
  actionError: string | null;
  /** 已请求取消但服务端尚未确认（运行仍在继续）。 */
  isCancelPending: boolean;
  openRun: (runId: string) => Promise<void>;
  cancelRun: () => Promise<void>;
  resumeRun: (payload: AgentRunResume) => Promise<void>;
  reportError: (message: string) => void;
  clearError: () => void;
}
