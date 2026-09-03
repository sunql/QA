import { httpClient } from "./client";
import type {
    AgentToolConfig,
    AgentToolConfigCreate,
    AgentToolConfigUpdate,
} from "../types/agentTool";

const PREFIX = "/agent-tools";

export async function listAgentTools(
    params: { enabledOnly?: boolean } = {},
): Promise<AgentToolConfig[]> {
    const res = await httpClient.get<AgentToolConfig[]>(PREFIX, {
        params: params.enabledOnly ? { enabledOnly: "true" } : {},
    });
    return res.data;
}

export async function getAgentTool(name: string): Promise<AgentToolConfig> {
    const res = await httpClient.get<AgentToolConfig>(`${PREFIX}/${name}`);
    return res.data;
}

export async function createAgentTool(
    payload: AgentToolConfigCreate,
): Promise<AgentToolConfig> {
    const res = await httpClient.post<AgentToolConfig>(PREFIX, payload);
    return res.data;
}

export async function updateAgentTool(
    name: string,
    payload: AgentToolConfigUpdate,
): Promise<AgentToolConfig> {
    const res = await httpClient.put<AgentToolConfig>(
        `${PREFIX}/${name}`,
        payload,
    );
    return res.data;
}

export async function deleteAgentTool(name: string): Promise<void> {
    await httpClient.delete(`${PREFIX}/${name}`);
}

export async function toggleAgentTool(
    name: string,
    enabled: boolean,
): Promise<AgentToolConfig> {
    const res = await httpClient.post<AgentToolConfig>(
        `${PREFIX}/${name}/toggle`,
        { enabled },
    );
    return res.data;
}