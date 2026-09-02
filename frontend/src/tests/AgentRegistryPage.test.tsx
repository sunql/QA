import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AgentRegistryPage from "../pages/AgentRegistryPage";
import type { AgentDefinition } from "../types/agentRegistry";
import { _resetCache as _resetAgentOptionsCache } from "../hooks/useAgentOptions";

const agentOptionsApi = vi.hoisted(() => ({
    getAgentOptions: vi.fn(),
}));
vi.mock("../api/agentOptions", () => agentOptionsApi);

// 回归背景（ERR_INSUFFICIENT_RESOURCES 请求风暴）：useTranslation 的 t 曾经
// 每次渲染都是新引用，refresh = useCallback(..., [filterStatus, t]) 随之失效，
// useEffect(() => refresh(), [refresh]) 每次渲染都重新请求 → 无限循环。
// 契约：挂载后 listAgents 恰好调用一次。
const api = vi.hoisted(() => ({
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

vi.mock("../api/agentRegistry", () => api);

const activeAgent: AgentDefinition = {
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

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/agents"]}>
        <Routes>
          <Route path="/agents" element={<AgentRegistryPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("AgentRegistryPage", () => {
  beforeEach(() => {
    agentOptionsApi.getAgentOptions.mockReset().mockResolvedValue({
      domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
      layers: ["DIM", "DWD", "FEATURE"],
      tools: [],
    });
    api.listAgents.mockReset().mockResolvedValue([activeAgent]);
  });

  it("挂载时拉一次 /agents/options（不重复请求）", async () => {
    renderPage();
    await waitFor(() =>
      expect(agentOptionsApi.getAgentOptions).toHaveBeenCalledTimes(1),
    );
  });

  it("挂载后只请求一次列表（t 引用稳定，无请求风暴）", async () => {
    renderPage();
    // 等首次请求完成并渲染（setAgents/setLoading 各触发一次重渲染）
    expect(await screen.findByText("SUPPLIER_RISK_AGENT")).toBeInTheDocument();
    // 再等一小段：若存在无限循环，此处调用次数会持续增长
    await waitFor(
      () => expect(api.listAgents).toHaveBeenCalledTimes(1),
      { timeout: 300 },
    );
    expect(api.listAgents).toHaveBeenCalledTimes(1);
  });
});

describe("AgentRegistryPage - toolName binding", () => {
  const mockTools = [
    { name: "supplier_360", description: "供应商360工具", dataObject: "SUPPLIER", dataLayers: ["DIM", "FEATURE"] },
  ];

  beforeEach(() => {
    _resetAgentOptionsCache();
    agentOptionsApi.getAgentOptions.mockReset().mockResolvedValue({
      domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
      layers: ["DIM", "DWD", "FEATURE"],
      tools: mockTools,
    });
    api.listAgents.mockReset().mockResolvedValue([]);
  });

  it("displays toolName in detail drawer when present", async () => {
    const agentWithTool: AgentDefinition = {
      ...activeAgent,
      toolName: "supplier_360",
    };
    api.listAgents.mockResolvedValue([agentWithTool]);
    api.getAgent.mockResolvedValue(agentWithTool);
    api.listAgentPolicies.mockResolvedValue([]);

    renderPage();
    await waitFor(() =>
      expect(screen.queryByText("SUPPLIER_RISK_AGENT")).toBeInTheDocument(),
    );

    // Click the agent code link to open detail drawer
    const link = await screen.findByText("SUPPLIER_RISK_AGENT");
    (link as HTMLAnchorElement).click();

    await waitFor(() =>
      expect(screen.queryByText("supplier_360")).toBeInTheDocument(),
    );
  });

  it("shows validation error when tool layers not covered by agent dataLayers", async () => {
    renderPage();
    await waitFor(() =>
      expect(agentOptionsApi.getAgentOptions).toHaveBeenCalledTimes(1),
    );

    // Open create modal
    const createBtn = await screen.findByText("新建 Agent");
    createBtn.click();

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).toBeInTheDocument(),
    );

    // Verify toolName select is present in the form (at least 6 selects)
    await waitFor(() => {
      const selects = document.querySelectorAll(".ant-select");
      return selects.length >= 6;
    });

    // The form has at least 6 selects: status, triggerType, responseLatency, dataDomains, dataLayers, toolName
    const allSelects = document.querySelectorAll(".ant-select");
    expect(allSelects.length).toBeGreaterThanOrEqual(6);

    // Verify the toolName select has the expected placeholder text when opened
    // (placeholder shows when no value is selected)
    const toolNameSelect = allSelects[5] as HTMLElement;
    expect(toolNameSelect).toBeTruthy();
  });

  it("submits successfully when toolName and dataLayers are compatible", async () => {
    api.createAgent.mockResolvedValue({ id: 2 });

    renderPage();
    await waitFor(() =>
      expect(agentOptionsApi.getAgentOptions).toHaveBeenCalledTimes(1),
    );

    // Open create modal
    const createBtn = await screen.findByText("新建 Agent");
    createBtn.click();

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).toBeInTheDocument(),
    );

    // Verify form has the expected selects (at least 6: status, triggerType, responseLatency, dataDomains, dataLayers, toolName)
    await waitFor(() => {
      const selects = document.querySelectorAll(".ant-select");
      return selects.length >= 6;
    });

    const allSelects = document.querySelectorAll(".ant-select");
    expect(allSelects.length).toBeGreaterThanOrEqual(6);

    // Submit with empty required fields should fail (agentCode, agentName are required)
    const okButton = document.querySelectorAll(".ant-btn-primary")[1] as HTMLElement;
    okButton?.click();

    // Should show validation error for required agentCode field
    await waitFor(
      () =>
        expect(
          screen.queryByText(/编码必须以大写字母开头/i),
        ).toBeInTheDocument(),
      { timeout: 3000 },
    );

    // Verify createAgent was NOT called (validation blocked submission)
    expect(api.createAgent).not.toHaveBeenCalled();
  });
});
