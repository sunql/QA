export interface AgentToolOption {
    name: string;
    description: string;
    dataObject: string;
    dataLayers: string[];
}

export interface AgentOptions {
    domains: string[];
    layers: string[];
    tools: AgentToolOption[];
}
