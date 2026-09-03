import { describe, it, expect } from "vitest";
import type {
    AgentToolConfig,
    AgentToolConfigCreate,
    AgentToolConfigUpdate,
} from "../types/agentTool";
import {
    listAgentTools,
    getAgentTool,
    createAgentTool,
    updateAgentTool,
    deleteAgentTool,
    toggleAgentTool,
} from "../api/agentTools";

describe("agentTool types", () => {
    it("AgentToolConfig has all required fields", () => {
        const sample: AgentToolConfig = {
            id: 1,
            name: "supplier_360",
            description: "x",
            dataObject: "SUPPLIER",
            dataLayers: ["DIM", "FEATURE"],
            inputSchema: {},
            handlerKind: "BUILTIN",
            handlerRef: "supplier_360",
            argExtractorKind: "supplier_key",
            enabled: true,
            version: 1,
            createdTime: "2026-09-03T00:00:00Z",
            updatedTime: null,
        };
        expect(sample.name).toBe("supplier_360");
    });

    it("AgentToolConfigCreate omits id/version/audit fields", () => {
        const sample: AgentToolConfigCreate = {
            name: "foo",
            dataObject: "SUPPLIER",
            dataLayers: [],
            inputSchema: {},
            handlerKind: "BUILTIN",
            handlerRef: "supplier_360",
            argExtractorKind: "supplier_key",
        };
        expect((sample as { id?: unknown }).id).toBeUndefined();
    });

    it("AgentToolConfigUpdate requires version", () => {
        const sample: AgentToolConfigUpdate = { version: 1 };
        expect(sample.version).toBe(1);
    });
});

describe("agentTools API client exports", () => {
    it("exports all 6 functions", () => {
        expect(typeof listAgentTools).toBe("function");
        expect(typeof getAgentTool).toBe("function");
        expect(typeof createAgentTool).toBe("function");
        expect(typeof updateAgentTool).toBe("function");
        expect(typeof deleteAgentTool).toBe("function");
        expect(typeof toggleAgentTool).toBe("function");
    });
});
