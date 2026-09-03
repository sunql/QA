export type AgentToolHandlerKind = "BUILTIN" | "NL2SQL";

export interface AgentToolConfig {
    id: number;
    name: string;
    description: string | null;
    dataObject: string;
    dataLayers: string[];
    inputSchema: Record<string, unknown>;
    handlerKind: AgentToolHandlerKind;
    handlerRef: string;
    argExtractorKind: string;
    enabled: boolean;
    version: number;
    createdTime: string;
    updatedTime: string | null;
}

export interface AgentToolConfigCreate {
    name: string;
    description?: string | null;
    dataObject: string;
    dataLayers?: string[];
    inputSchema?: Record<string, unknown>;
    handlerKind: AgentToolHandlerKind;
    handlerRef: string;
    argExtractorKind?: string;
}

export interface AgentToolConfigUpdate {
    description?: string | null;
    dataObject?: string;
    dataLayers?: string[];
    inputSchema?: Record<string, unknown>;
    handlerKind?: AgentToolHandlerKind;
    handlerRef?: string;
    argExtractorKind?: string;
    enabled?: boolean;
    /** Optimistic lock version（创建后端会 bump +1） */
    version: number;
}