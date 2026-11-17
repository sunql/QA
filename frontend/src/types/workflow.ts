/** P5 工作流 / 审批前端类型（与后端 `app/domain/workflow_schemas.py` 一一对应）。
 *
 * 后端 DTO 是 `CamelModel` ⇒ JSON 一律 camelCase；本文件按 JSON 形态声明。
 */

/** 与 `app/services/workflow_state_machine.py` 的 instance 矩阵逐字一致。 */
export type WorkflowInstanceStatus =
  | "PENDING"
  | "RUNNING"
  | "WAITING_APPROVAL"
  | "APPROVED"
  | "REJECTED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED"
  | "COMPENSATED";

export type WorkflowApprovalStatus =
  | "PENDING"
  | "APPROVED"
  | "REJECTED"
  | "TIMEOUT"
  | "CANCELLED";

export type WorkflowStepStatus =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "SKIPPED"
  | "COMPENSATED";

export type WorkflowDecision = "APPROVE" | "REJECT";

/** 已发布定义摘要（提交表单候选集；后端**不下发** definitionJson）。 */
export interface WorkflowDefinitionSummary {
  id: number;
  domainCode: string;
  capabilityCode: string;
  version: string;
  status: string;
  createdBy: string | null;
  createdTime: string;
}

/** 实例列表行（带定义上下文，避免页面只显示裸 definitionId）。 */
export interface WorkflowInstanceListItem {
  id: number;
  definitionId: number;
  domainCode: string;
  capabilityCode: string;
  definitionVersion: string;
  status: string;
  idempotencyKey: string;
  createdBy: string | null;
  errorMessage: string | null;
  createdTime: string;
  updatedTime: string;
}

export interface WorkflowInstancePage {
  rows: WorkflowInstanceListItem[];
  total: number;
}

/** 实例摘要（详情接口 / 提交 / 取消的返回体）。 */
export interface WorkflowInstance {
  id: number;
  definitionId: number;
  status: string;
  idempotencyKey: string;
  createdBy: string | null;
  errorMessage: string | null;
  createdTime: string;
  updatedTime: string;
}

export interface WorkflowStep {
  id: number;
  stepCode: string;
  stepOrder: number;
  status: string;
  errorMessage: string | null;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface WorkflowApproval {
  id: number;
  instanceId: number;
  matrixCode: string;
  subjectType: string;
  subjectId: string;
  status: string;
  approverRole: string | null;
  approverUser: string | null;
  decisionNote: string | null;
  dueAt: string | null;
  decidedAt: string | null;
  createdBy: string | null;
}

export interface WorkflowInstanceDetail {
  instance: WorkflowInstance;
  steps: WorkflowStep[];
  approvals: WorkflowApproval[];
}

/** 「我的待办」行：审批任务 + 实例上下文。 */
export interface WorkflowApprovalListItem extends WorkflowApproval {
  domainCode: string;
  capabilityCode: string;
  definitionVersion: string;
  instanceStatus: string;
  instanceCreatedBy: string | null;
}

export interface WorkflowApprovalPage {
  rows: WorkflowApprovalListItem[];
  total: number;
}

export interface WorkflowInstanceCreate {
  domainCode: string;
  capabilityCode: string;
  version: string;
  idempotencyKey: string;
  request: Record<string, unknown>;
}

export interface WorkflowApprovalDecision {
  decision: WorkflowDecision;
  note?: string | null;
}
