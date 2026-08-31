// Agent Registry 类型（Phase 6.1）

export type AgentTriggerType = "user_question" | "scheduled" | "event";

export type AgentResponseLatency = "realtime" | "batch";

export type AgentStatus = "active" | "draft" | "deprecated";

export type AgentPermission = "read" | "masked_read" | "forbidden" | "forbidden_write";

export interface AgentAccessPolicy {
  id: number;
  dataObject: string;
  permission: AgentPermission;
  dataLayer: string | null;
  notes: string | null;
  createdTime?: string | null;
}

export interface AgentAccessPolicyCreate {
  dataObject: string;
  permission?: AgentPermission;
  dataLayer?: string | null;
  notes?: string | null;
}

export interface AgentAccessPolicyUpdate {
  permission?: AgentPermission;
  dataLayer?: string | null;
  notes?: string | null;
}

export interface AgentDefinition {
  id: number;
  agentCode: string;
  agentName: string;
  description: string | null;
  triggerType: AgentTriggerType;
  responseLatency: AgentResponseLatency;
  dataDomains: string[];
  dataLayers: string[];
  status: AgentStatus;
  owner: string | null;
  version: string;
  policies: AgentAccessPolicy[];
  createdTime?: string | null;
  updatedTime?: string | null;
}

export interface AgentDefinitionCreate {
  agentCode: string;
  agentName: string;
  description?: string | null;
  triggerType?: AgentTriggerType;
  responseLatency?: AgentResponseLatency;
  dataDomains?: string[];
  dataLayers?: string[];
  status?: AgentStatus;
  version?: string | null;
  policies?: AgentAccessPolicyCreate[];
}

export interface AgentDefinitionUpdate {
  agentName?: string;
  description?: string | null;
  triggerType?: AgentTriggerType;
  responseLatency?: AgentResponseLatency;
  dataDomains?: string[];
  dataLayers?: string[];
  status?: AgentStatus;
  version?: string | null;
}