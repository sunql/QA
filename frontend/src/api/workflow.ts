/** P5 工作流 / 审批 API（后端 `app/api/v1/workflow.py`）。
 *
 * ⚠️ 整组接口受后端开关 `ENABLE_WORKFLOW` 控制：关闭时路由不注册 ⇒ 404。
 * 前端不需要自行探测，404 按普通错误提示即可（"工作流功能未开启"）。
 */

import { httpClient } from "./client";
import type {
  WorkflowApproval,
  WorkflowApprovalDecision,
  WorkflowApprovalPage,
  WorkflowDefinitionAdminPage,
  WorkflowDefinitionAdminRead,
  WorkflowDefinitionStatus,
  WorkflowDefinitionSummary,
  WorkflowDefinitionWrite,
  WorkflowInstance,
  WorkflowInstanceCreate,
  WorkflowInstanceDetail,
  WorkflowInstancePage,
} from "../types/workflow";

const BASE = "/workflow";

export interface WorkflowInstanceListParams {
  status?: string;
  limit?: number;
  offset?: number;
}

export async function listWorkflowInstances(
  params: WorkflowInstanceListParams = {},
): Promise<WorkflowInstancePage> {
  const res = await httpClient.get<WorkflowInstancePage>(`${BASE}/instances`, {
    params,
  });
  return res.data;
}

export async function getWorkflowInstance(
  instanceId: number,
): Promise<WorkflowInstanceDetail> {
  const res = await httpClient.get<WorkflowInstanceDetail>(
    `${BASE}/instances/${instanceId}`,
  );
  return res.data;
}

export async function submitWorkflowInstance(
  payload: WorkflowInstanceCreate,
): Promise<WorkflowInstance> {
  const res = await httpClient.post<WorkflowInstance>(
    `${BASE}/instances`,
    payload,
  );
  return res.data;
}

export async function cancelWorkflowInstance(
  instanceId: number,
  reason?: string,
): Promise<WorkflowInstance> {
  const res = await httpClient.post<WorkflowInstance>(
    `${BASE}/instances/${instanceId}/cancel`,
    { reason: reason ?? null },
  );
  return res.data;
}

export interface WorkflowApprovalListParams {
  status?: string;
  limit?: number;
  offset?: number;
}

/** 我的待办（默认只回 PENDING；要求后端已校验身份）。 */
export async function listMyApprovals(
  params: WorkflowApprovalListParams = {},
): Promise<WorkflowApprovalPage> {
  const res = await httpClient.get<WorkflowApprovalPage>(`${BASE}/approvals`, {
    params,
  });
  return res.data;
}

export async function decideWorkflowApproval(
  taskId: number,
  payload: WorkflowApprovalDecision,
): Promise<WorkflowApproval> {
  const res = await httpClient.post<WorkflowApproval>(
    `${BASE}/approvals/${taskId}/decision`,
    payload,
  );
  return res.data;
}

/** 已发布定义（提交表单候选集）。 */
export async function listWorkflowDefinitions(
  limit = 100,
): Promise<WorkflowDefinitionSummary[]> {
  const res = await httpClient.get<WorkflowDefinitionSummary[]>(
    `${BASE}/definitions`,
    { params: { limit } },
  );
  return res.data;
}

/* ------------------------------------------------------------------ */
/* 定义管理面（admin-only 写入口）                                       */
/* ------------------------------------------------------------------ */

export interface WorkflowDefinitionAdminListParams {
  status?: WorkflowDefinitionStatus;
  limit?: number;
  offset?: number;
}

/** 全部状态的定义（含定义体；后端仅 admin 可见）。 */
export async function listWorkflowDefinitionAdmin(
  params: WorkflowDefinitionAdminListParams = {},
): Promise<WorkflowDefinitionAdminPage> {
  const res = await httpClient.get<WorkflowDefinitionAdminPage>(
    `${BASE}/admin/definitions`,
    { params },
  );
  return res.data;
}

export async function createWorkflowDefinition(
  payload: WorkflowDefinitionWrite,
): Promise<WorkflowDefinitionAdminRead> {
  const res = await httpClient.post<WorkflowDefinitionAdminRead>(
    `${BASE}/admin/definitions`,
    payload,
  );
  return res.data;
}

export async function updateWorkflowDefinition(
  definitionId: number,
  payload: WorkflowDefinitionWrite,
): Promise<WorkflowDefinitionAdminRead> {
  const res = await httpClient.put<WorkflowDefinitionAdminRead>(
    `${BASE}/admin/definitions/${definitionId}`,
    payload,
  );
  return res.data;
}

export async function publishWorkflowDefinition(
  definitionId: number,
): Promise<WorkflowDefinitionAdminRead> {
  const res = await httpClient.post<WorkflowDefinitionAdminRead>(
    `${BASE}/admin/definitions/${definitionId}/publish`,
    {},
  );
  return res.data;
}

export async function disableWorkflowDefinition(
  definitionId: number,
): Promise<WorkflowDefinitionAdminRead> {
  const res = await httpClient.post<WorkflowDefinitionAdminRead>(
    `${BASE}/admin/definitions/${definitionId}/disable`,
    {},
  );
  return res.data;
}
