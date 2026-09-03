import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AdminAuditPage from "../pages/AdminAuditPage";
import AgentRegistryPage from "../pages/AgentRegistryPage";
import type { AuditLog } from "../types/audit";
import type { AgentDefinition } from "../types/agentRegistry";
import { _resetCache as _resetAgentOptionsCache } from "../hooks/useAgentOptions";

const auditApi = vi.hoisted(() => ({
    listAuditLogs: vi.fn(),
    exportAuditLogs: vi.fn(),
}));
vi.mock("../api/audit", () => auditApi);

const agentOptionsApi = vi.hoisted(() => ({
    getAgentOptions: vi.fn(),
}));
vi.mock("../api/agentOptions", () => agentOptionsApi);

const agentRegistryApi = vi.hoisted(() => ({
    listAgents: vi.fn(),
    getAgent: vi.fn(),
    listAgentPolicies: vi.fn(),
    createAgent: vi.fn(),
    updateAgent: vi.fn(),
    deprecateAgent: vi.fn(),
    addAgentPolicy: vi.fn(),
    updateAgentPolicy: vi.fn(),
    deleteAgentPolicy: vi.fn(),
}));
vi.mock("../api/agentRegistry", () => agentRegistryApi);

const mockLog: AuditLog = {
    id: 1,
    entityType: "AGENT_TOOL_CONFIG",
    entityId: 5,
    action: "UPDATE",
    actor: "admin",
    actorDepartments: "platform",
    beforeJson: null,
    afterJson: { name: "supplier_360", enabled: true },
    createdAt: "2026-09-03T10:00:00Z",
};

const mockAgent: AgentDefinition = {
    id: 1,
    agentCode: "SUPPLIER_RISK_AGENT",
    agentName: "供应商风险 Agent",
    description: null,
    triggerType: "user_question",
    responseLatency: "realtime",
    dataDomains: ["PROCUREMENT"],
    dataLayers: ["FEATURE"],
    status: "active",
    owner: "procurement",
    version: "v1.0",
    policies: [],
    runnable: true,
    toolName: null,
};

function renderAudit() {
    return render(
        <ConfigProvider locale={zhCN}>
            <AdminAuditPage />
        </ConfigProvider>,
    );
}

function renderRegistry() {
    return render(
        <ConfigProvider locale={zhCN}>
            <MemoryRouter initialEntries={["/agents"]}>
                <Routes>
                    <Route path="/agents" element={<AgentRegistryPage />} />
                </Routes>
            </MemoryRouter>
        </ConfigProvider>,
    );
}

describe("T16 — AdminAuditPage exposes AGENT_TOOL_CONFIG option", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        auditApi.listAuditLogs.mockResolvedValue({ rows: [mockLog], total: 1 });
        auditApi.exportAuditLogs.mockResolvedValue(
            new Blob(["id\n"], { type: "text/csv" }),
        );
    });

    it("AGENT_TOOL_CONFIG option exists in entityType filter options", async () => {
        // antd v5 Select 默认 virtual=true，jsdom 下 VirtualList 仅渲染
        // ~10 项；AGENT_TOOL_CONFIG 在第 11 位可能未进入可见窗口，
        // 因此用契约性断言：源文件的 ENTITY_TYPE_OPTIONS 数组包含
        // AGENT_TOOL_CONFIG——这正是 Select 的 options 源。
        const fs = await import("fs");
        const path = await import("path");
        const srcPath = path.resolve(
            process.cwd(),
            "src/pages/AdminAuditPage.tsx",
        );
        const src = fs.readFileSync(srcPath, "utf8");
        const block = src.match(
            /const ENTITY_TYPE_OPTIONS = \[([\s\S]*?)\];/,
        );
        expect(block, "ENTITY_TYPE_OPTIONS block").toBeTruthy();
        expect(block![1]).toContain("AGENT_TOOL_CONFIG");

        // 同时确认运行时已挂载（无运行时崩溃）
        renderAudit();
        await waitFor(() => expect(auditApi.listAuditLogs).toHaveBeenCalled());
    });
});

describe("AgentRegistryPage — dataObject field sources from useAgentOptions", () => {
    beforeEach(() => {
        _resetAgentOptionsCache();
        agentOptionsApi.getAgentOptions.mockReset().mockResolvedValue({
            domains: ["PROCUREMENT"],
            layers: ["DIM", "DWD", "FEATURE"],
            tools: [
                {
                    name: "supplier_360",
                    description: "x",
                    dataObject: "SUPPLIER",
                    dataLayers: ["DIM", "FEATURE"],
                },
                {
                    name: "graph_traverse",
                    description: "y",
                    dataObject: "GRAPH",
                    dataLayers: ["DIM", "DWD"],
                },
            ],
        });
        agentRegistryApi.listAgents.mockReset().mockResolvedValue([mockAgent]);
        agentRegistryApi.getAgent.mockReset().mockResolvedValue(mockAgent);
        agentRegistryApi.listAgentPolicies.mockReset().mockResolvedValue([]);
    });

    it("policy form dataObject renders a Select with options from useAgentOptions", async () => {
        const user = userEvent.setup();
        renderRegistry();
        await waitFor(() =>
            expect(agentOptionsApi.getAgentOptions).toHaveBeenCalled(),
        );
        // Click agent code link to open the detail drawer (which contains the policies form)
        const codeLink = await screen.findByText("SUPPLIER_RISK_AGENT");
        await user.click(codeLink);

        // The policies title is rendered inside the drawer
        expect(
            await screen.findByText("数据访问策略"),
        ).toBeTruthy();

        // Find the dataObject Select by its placeholder (Form.Item style doesn't
        // transfer to inner Select's inline style; use placeholder text instead).
        const dataObjectPlaceholder = await screen.findByText("如 SUPPLIER");
        // Walk up to the closest .ant-select container
        let node: HTMLElement | null = dataObjectPlaceholder as HTMLElement;
        while (node && !node.classList?.contains("ant-select")) {
            node = node.parentElement;
        }
        expect(node).toBeTruthy();
        // Click the inner .ant-select-selector (the clickable area)
        const selector = node!.querySelector(
            ".ant-select-selector",
        ) as HTMLElement;
        expect(selector).toBeTruthy();
        await user.click(selector);

        await waitFor(() => {
            const items = document.querySelectorAll(".ant-select-item-option");
            const labels = Array.from(items).map((el) => el.textContent || "");
            expect(labels).toContain("SUPPLIER");
            expect(labels).toContain("GRAPH");
        });
    });
});