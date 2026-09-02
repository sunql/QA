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
  /** Phase 6.4 派生字段：SSOT 是否可通过 POST /agents/{code}/run 调用。
   *  true = status=ACTIVE 且已注册到 AGENT_TOOLS；元数据占位 agent（如
   *  SUPPLIER_OTD_REPORT / PROCUREMENT_COPILOT）即使 status=active 也为 false。 */
  runnable?: boolean;
  /** Phase 6.4 绑定工具名：来自 useAgentOptions().tools，null 表示元数据占位 Agent。 */
  toolName?: string | null;
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
  toolName?: string | null;
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
  toolName?: string | null;
}