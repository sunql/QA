import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AgentRegistryPage from "../pages/AgentRegistryPage";
import type { AgentDefinition } from "../types/agentRegistry";

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
    api.listAgents.mockReset().mockResolvedValue([activeAgent]);
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
