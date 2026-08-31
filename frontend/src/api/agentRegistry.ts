import { httpClient } from "./client";
import type {
  AgentAccessPolicy,
  AgentAccessPolicyCreate,
  AgentAccessPolicyUpdate,
  AgentDefinition,
  AgentDefinitionCreate,
  AgentDefinitionUpdate,
  AgentStatus,
} from "../types/agentRegistry";
import type { AgentRunRead } from "../types/agentRuntime";

const PREFIX = "/agents";

export async function listAgents(params?: {
  status?: AgentStatus;
  dataDomain?: string;
}): Promise<AgentDefinition[]> {
  const q: Record<string, string> = {};
  if (params?.status) q.status = params.status;
  if (params?.dataDomain) q.dataDomain = params.dataDomain;
  const res = await httpClient.get<AgentDefinition[]>(PREFIX, { params: q });
  return res.data;
}

export async function getAgent(agentCode: string): Promise<AgentDefinition> {
  const res = await httpClient.get<AgentDefinition>(`${PREFIX}/${agentCode}`);
  return res.data;
}

export async function createAgent(
  payload: AgentDefinitionCreate,
): Promise<AgentDefinition> {
  const res = await httpClient.post<AgentDefinition>(PREFIX, payload);
  return res.data;
}

export async function updateAgent(
  agentCode: string,
  payload: AgentDefinitionUpdate,
): Promise<AgentDefinition> {
  const res = await httpClient.put<AgentDefinition>(
    `${PREFIX}/${agentCode}`,
    payload,
  );
  return res.data;
}

export async function deprecateAgent(
  agentCode: string,
): Promise<AgentDefinition> {
  const res = await httpClient.delete<AgentDefinition>(
    `${PREFIX}/${agentCode}`,
  );
  return res.data;
}

export async function listAgentPolicies(
  agentCode: string,
): Promise<AgentAccessPolicy[]> {
  const res = await httpClient.get<AgentAccessPolicy[]>(
    `${PREFIX}/${agentCode}/policies`,
  );
  return res.data;
}

export async function addAgentPolicy(
  agentCode: string,
  payload: AgentAccessPolicyCreate,
): Promise<AgentAccessPolicy> {
  const res = await httpClient.post<AgentAccessPolicy>(
    `${PREFIX}/${agentCode}/policies`,
    payload,
  );
  return res.data;
}

export async function updateAgentPolicy(
  agentCode: string,
  policyId: number,
  payload: AgentAccessPolicyUpdate,
): Promise<AgentAccessPolicy> {
  const res = await httpClient.put<AgentAccessPolicy>(
    `${PREFIX}/${agentCode}/policies/${policyId}`,
    payload,
  );
  return res.data;
}

export async function deleteAgentPolicy(
  agentCode: string,
  policyId: number,
): Promise<void> {
  await httpClient.delete(`${PREFIX}/${agentCode}/policies/${policyId}`);
}

// Phase 6.4：Agent 运行时执行（POST /agents/{code}/run）
// 成功返回 AgentRunRead；失败由全局 DomainError handler 映射（404/409/403/422），
// httpClient 拦截器转为携带 .status 的 Error，页面按状态分流。
export async function runAgent(
  agentCode: string,
  input: string,
): Promise<AgentRunRead> {
  const res = await httpClient.post<AgentRunRead>(`${PREFIX}/${agentCode}/run`, {
    input,
  });
  return res.data;
}