import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AgentRuntimePage from "../pages/AgentRuntimePage";
import type { AgentDefinition } from "../types/agentRegistry";
import type { AgentRunRead } from "../types/agentRuntime";

const api = vi.hoisted(() => ({
  listAgents: vi.fn(),
  runAgent: vi.fn(),
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

const draftAgent: AgentDefinition = {
  ...activeAgent,
  id: 2,
  agentCode: "DRAFT_AGENT",
  agentName: "草稿 Agent",
  status: "draft",
  runnable: false,
};

/** Phase 6.4：元数据占位 Agent（如 SUPPLIER_OTD_REPORT）—— status=active 但未绑定工具，
 *  后端 runnable=false；前端 Runtime 页必须过滤掉，避免用户点出 409。 */
const metadataOnlyAgent: AgentDefinition = {
  ...activeAgent,
  id: 3,
  agentCode: "SUPPLIER_OTD_REPORT",
  agentName: "供应商 OTD 报表 Agent",
  runnable: false,
};

const runRead: AgentRunRead = {
  agentCode: "SUPPLIER_RISK_AGENT",
  agentName: "供应商风险 Agent",
  agentOwner: "procurement",
  tool: "supplier_risk",
  result: {
    level: "high",
    levelSource: "risk_score",
    profile: {
      enterpriseKey: 100001,
      enterpriseCode: "SUP000001",
      owner: "procurement",
      matchRule: "MDM_MASTER",
      effectiveDate: "2026-01-01",
      expiryDate: null,
    },
    contributions: [],
    riskPoints: null,
    riskPointsSource: "fallback_template",
    recommendedActions: [],
    tokensUsed: 0,
    cost: 0,
    llmModelName: null,
    fetchedAt: "2026-08-31T00:00:00Z",
  },
  answer: "供应商 **SUP000001** 风险等级：high。",
  tokensUsed: 0,
  promptTokens: 0,
  completionTokens: 0,
  cost: 0,
  llmModelName: null,
  executedAt: "2026-08-31T00:00:00Z",
};

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/agents/run"]}>
        <Routes>
          <Route path="/agents/run" element={<AgentRuntimePage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("AgentRuntimePage", () => {
  beforeEach(() => {
    api.listAgents.mockResolvedValue([activeAgent, draftAgent, metadataOnlyAgent]);
    api.runAgent.mockResolvedValue(runRead);
  });

  it("加载 Agent 列表并仅展示 runnable=true Agent（过滤草稿/元数据占位）", async () => {
    renderPage();
    await waitFor(() => expect(api.listAgents).toHaveBeenCalled());
    // 打开下拉：runnable Agent 在选项中，DRAFT 与 metadata-only 都被过滤
    fireEvent.mouseDown(document.querySelector(".ant-select-selector") as HTMLElement);
    await screen.findByText("SUPPLIER_RISK_AGENT（供应商风险 Agent）");
    expect(screen.queryByText("DRAFT_AGENT（草稿 Agent）")).toBeNull();
    // 元数据占位 Agent（status=active 但未绑定工具）也被过滤
    expect(
      screen.queryByText("SUPPLIER_OTD_REPORT（供应商 OTD 报表 Agent）"),
    ).toBeNull();
  });

  it("未选择 Agent 直接运行 → 提示先选择 Agent", async () => {
    renderPage();
    await waitFor(() => expect(api.listAgents).toHaveBeenCalled());
    fireEvent.change(
      screen.getByPlaceholderText("如：评估供应商 10105 的风险"),
      { target: { value: "评估供应商 10105" } },
    );
    // antd 对双汉字按钮自动插入空格（autoInsertSpaceInButton → "运 行"），用 role+正则匹配
    fireEvent.click(screen.getByRole("button", { name: /运\s*行/ }));
    expect(await screen.findByText("请先选择一个 Agent")).toBeInTheDocument();
    expect(api.runAgent).not.toHaveBeenCalled();
  });

  it("选择 Agent + 输入 + 运行 → 渲染 AgentResponseCard", async () => {
    renderPage();
    await waitFor(() => expect(api.listAgents).toHaveBeenCalled());
    // 打开下拉并选择 ACTIVE Agent
    fireEvent.mouseDown(document.querySelector(".ant-select-selector") as HTMLElement);
    fireEvent.click(
      await screen.findByText("SUPPLIER_RISK_AGENT（供应商风险 Agent）"),
    );
    fireEvent.change(
      screen.getByPlaceholderText("如：评估供应商 10105 的风险"),
      { target: { value: "评估供应商 10105 的风险" } },
    );
    // antd 对双汉字按钮自动插入空格（autoInsertSpaceInButton → "运 行"），用 role+正则匹配
    fireEvent.click(screen.getByRole("button", { name: /运\s*行/ }));
    await waitFor(() => expect(api.runAgent).toHaveBeenCalledWith("SUPPLIER_RISK_AGENT", "评估供应商 10105 的风险"));
    // AgentResponseCard 头部
    expect(await screen.findByText("供应商风险 Agent")).toBeInTheDocument();
    expect(screen.getByText("supplier_risk")).toBeInTheDocument();
  });

  it("运行 404 → 友好「未注册」消息", async () => {
    api.runAgent.mockRejectedValue(Object.assign(new Error("not found"), { status: 404 }));
    renderPage();
    await waitFor(() => expect(api.listAgents).toHaveBeenCalled());
    fireEvent.mouseDown(document.querySelector(".ant-select-selector") as HTMLElement);
    fireEvent.click(
      await screen.findByText("SUPPLIER_RISK_AGENT（供应商风险 Agent）"),
    );
    fireEvent.change(
      screen.getByPlaceholderText("如：评估供应商 10105 的风险"),
      { target: { value: "随便看看" } },
    );
    // antd 对双汉字按钮自动插入空格（autoInsertSpaceInButton → "运 行"），用 role+正则匹配
    fireEvent.click(screen.getByRole("button", { name: /运\s*行/ }));
    expect(
      await screen.findByText("Agent SUPPLIER_RISK_AGENT 未注册"),
    ).toBeInTheDocument();
  });
});
