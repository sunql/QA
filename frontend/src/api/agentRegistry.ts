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