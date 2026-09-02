import { httpClient } from "./client";
import type { AgentOptions } from "../types/agentOptions";

export async function getAgentOptions(): Promise<AgentOptions> {
    const res = await httpClient.get<AgentOptions>("/agents/options");
    return res.data;
}
