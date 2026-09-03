import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import AdminToolsPage from "../pages/AdminToolsPage";

vi.mock("../api/agentTools", () => ({
    listAgentTools: vi.fn().mockResolvedValue([
        {
            id: 1,
            name: "supplier_360",
            description: "查询单供应商 360° 视图",
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
        },
    ]),
    createAgentTool: vi.fn(),
    updateAgentTool: vi.fn(),
    deleteAgentTool: vi.fn(),
    toggleAgentTool: vi.fn().mockResolvedValue({
        id: 1,
        name: "supplier_360",
        description: "查询单供应商 360° 视图",
        dataObject: "SUPPLIER",
        dataLayers: ["DIM", "FEATURE"],
        inputSchema: {},
        handlerKind: "BUILTIN",
        handlerRef: "supplier_360",
        argExtractorKind: "supplier_key",
        enabled: false,
        version: 2,
        createdTime: "2026-09-03T00:00:00Z",
        updatedTime: "2026-09-03T01:00:00Z",
    }),
    getAgentTool: vi.fn(),
}));

const renderWithProvider = (component: React.ReactNode) => {
    return render(<ConfigProvider>{component}</ConfigProvider>);
};

describe("AdminToolsPage", () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it("renders tool list from API", async () => {
        renderWithProvider(<AdminToolsPage />);
        const cells = await screen.findAllByText("supplier_360");
        expect(cells.length).toBeGreaterThan(0);
    });

    it("renders enabled switch per row", async () => {
        renderWithProvider(<AdminToolsPage />);
        await screen.findAllByText("supplier_360");
        const switches = document.querySelectorAll(".ant-switch");
        expect(switches.length).toBeGreaterThan(0);
    });

    it("exposes create + refresh buttons in toolbar", async () => {
        renderWithProvider(<AdminToolsPage />);
        await screen.findAllByText("supplier_360");
        // antd <Button> 在 inline 中文间插入空白，用 replace 折叠
        const buttons = screen
            .getAllByRole("button")
            .map((b) => (b.textContent || "").replace(/\s+/g, ""));
        expect(buttons).toContain("新建");
        expect(buttons).toContain("刷新");
    });

    it("exposes delete and edit actions per row", async () => {
        renderWithProvider(<AdminToolsPage />);
        await screen.findAllByText("supplier_360");
        const buttons = screen
            .getAllByRole("button")
            .map((b) => (b.textContent || "").replace(/\s+/g, ""));
        expect(buttons).toContain("编辑");
        expect(buttons).toContain("删除");
    });

    it("calls toggleAgentTool when switch toggled", async () => {
        const { toggleAgentTool } = await import("../api/agentTools");
        renderWithProvider(<AdminToolsPage />);
        await screen.findAllByText("supplier_360");
        const sw = document.querySelector(".ant-switch") as HTMLElement;
        expect(sw).toBeTruthy();
        fireEvent.click(sw);
        await waitFor(() => {
            expect(toggleAgentTool).toHaveBeenCalled();
        });
    });
});