import { httpClient } from "./client";
import type {
  AgentOrchestrationProfileCreate,
  AgentOrchestrationProfileRead,
  AgentOrchestrationProfileUpdate,
  AgentOrchestrationToggle,
  AgentOrchestrationToolOptionRead,
  AgentRunCreate,
  AgentRunCreated,
  AgentRunRead,
  AgentRunResume,
  AgentRunStepRead,
  RunStatus,
} from "../types/agentOrchestration";

const ORCHESTRATION_PREFIX = "/agent-orchestrations";
const RUN_PREFIX = "/agent-runs";

/** 列表运行的可选过滤条件；未定义的项不会被发送。 */
export interface AgentRunListFilters {
  agentCode?: string;
  status?: RunStatus;
  limit?: number;
  offset?: number;
}

/** 轨迹查询的可选参数；afterStepNo 为闭区间下界（step_no >= afterStepNo）。 */
export interface AgentRunStepQuery {
  afterStepNo?: number;
  limit?: number;
}

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
