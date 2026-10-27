import { httpClient } from "./client";
import type {
  AgentOrchestrationProfileCreate,
  AgentOrchestrationProfileRead,
  AgentOrchestrationProfileUpdate,
  AgentOrchestrationToggle,
  AgentOrchestrationToolOptionRead,
  AgentRunCreate,
  AgentRunCreated,
  AgentRunListFilters,
  AgentRunRead,
  AgentRunResume,
  AgentRunStepQuery,
  AgentRunStepRead,
} from "../types/agentOrchestration";

const ORCHESTRATION_PREFIX = "/agent-orchestrations";
const RUN_PREFIX = "/agent-runs";

// 查询参数类型定义在 types 模块（前后端契约集中地），这里再导出一次，
// 便于调用方只从 api 模块 import 即可拿到完整签名。
export type { AgentRunListFilters, AgentRunStepQuery };

// ---------------------------------------------------------------------------
// 编排配置
// ---------------------------------------------------------------------------

export async function createOrchestration(
  payload: AgentOrchestrationProfileCreate,
): Promise<AgentOrchestrationProfileRead> {
  const res = await httpClient.post<AgentOrchestrationProfileRead>(
    ORCHESTRATION_PREFIX,
    payload,
  );
  return res.data;
}

export async function listOrchestrations(): Promise<
  AgentOrchestrationProfileRead[]
> {
  const res = await httpClient.get<AgentOrchestrationProfileRead[]>(
    ORCHESTRATION_PREFIX,
  );
  return res.data;
}

export async function listOrchestrationToolOptions(): Promise<
  AgentOrchestrationToolOptionRead[]
> {
  const res = await httpClient.get<AgentOrchestrationToolOptionRead[]>(
    `${ORCHESTRATION_PREFIX}/tool-options`,
  );
  return res.data;
}

export async function getOrchestration(
  agentCode: string,
): Promise<AgentOrchestrationProfileRead> {
  const res = await httpClient.get<AgentOrchestrationProfileRead>(
    `${ORCHESTRATION_PREFIX}/${encodeURIComponent(agentCode)}`,
  );
  return res.data;
}

export async function updateOrchestration(
  agentCode: string,
  payload: AgentOrchestrationProfileUpdate,
): Promise<AgentOrchestrationProfileRead> {
  const res = await httpClient.put<AgentOrchestrationProfileRead>(
    `${ORCHESTRATION_PREFIX}/${encodeURIComponent(agentCode)}`,
    payload,
  );
  return res.data;
}

export async function toggleOrchestration(
  agentCode: string,
  payload: AgentOrchestrationToggle,
): Promise<AgentOrchestrationProfileRead> {
  const res = await httpClient.post<AgentOrchestrationProfileRead>(
    `${ORCHESTRATION_PREFIX}/${encodeURIComponent(agentCode)}/toggle`,
    payload,
  );
  return res.data;
}

// ---------------------------------------------------------------------------
// 运行
// ---------------------------------------------------------------------------

export async function createAgentRun(
  payload: AgentRunCreate,
): Promise<AgentRunCreated> {
  const res = await httpClient.post<AgentRunCreated>(RUN_PREFIX, payload);
  return res.data;
}

export async function listAgentRuns(
  filters?: AgentRunListFilters,
): Promise<AgentRunRead[]> {
  const params: Record<string, string | number> = {};
  if (filters?.agentCode !== undefined) params.agentCode = filters.agentCode;
  if (filters?.status !== undefined) params.status = filters.status;
  if (filters?.limit !== undefined) params.limit = filters.limit;
  if (filters?.offset !== undefined) params.offset = filters.offset;
  const res = await httpClient.get<AgentRunRead[]>(RUN_PREFIX, { params });
  return res.data;
}

export async function getAgentRun(runId: string): Promise<AgentRunRead> {
  const res = await httpClient.get<AgentRunRead>(
    `${RUN_PREFIX}/${encodeURIComponent(runId)}`,
  );
  return res.data;
}

/**
 * 查询运行轨迹。
 *
 * `query.afterStepNo` 是闭区间下界（`step_no >= afterStepNo`）：要只取比已持有的
 * 最后一步更新的步骤，调用方必须传 `lastStepNo + 1`；直接传 `lastStepNo` 会把
 * 最后一步重新取回，导致重复渲染。
 */
export async function getAgentRunSteps(
  runId: string,
  query?: AgentRunStepQuery,
): Promise<AgentRunStepRead[]> {
  const params: Record<string, number> = {};
  if (query?.afterStepNo !== undefined) params.afterStepNo = query.afterStepNo;
  if (query?.limit !== undefined) params.limit = query.limit;
  const res = await httpClient.get<AgentRunStepRead[]>(
    `${RUN_PREFIX}/${encodeURIComponent(runId)}/steps`,
    { params },
  );
  return res.data;
}

export async function resumeAgentRun(
  runId: string,
  payload: AgentRunResume,
): Promise<AgentRunRead> {
  const res = await httpClient.post<AgentRunRead>(
    `${RUN_PREFIX}/${encodeURIComponent(runId)}/resume`,
    payload,
  );
  return res.data;
}

export async function cancelAgentRun(runId: string): Promise<AgentRunRead> {
  const res = await httpClient.post<AgentRunRead>(
    `${RUN_PREFIX}/${encodeURIComponent(runId)}/cancel`,
  );
  return res.data;
}
