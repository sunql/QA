/** I3 采购 Copilot 页测试（mock API，真实 i18n）。

判据要点：
1. 场景只列 **runnable** 的 Agent；模型必须显式选（缺一不可发送）；
2. 发送后按 SSE 事件渲染：轨迹步骤 → 最终答案 → tokens；
3. **PAUSED 渲染成追问**（ASK_USER 的 planSummary），而不是「失败」；
4. 发送失败（HTTP 4xx）把后端 detail 展示出来，不是空白。
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { beforeEach, describe, expect, it, vi } from "vitest";
import i18n from "../../i18n/i18n";
import ProcurementCopilotPage from "../ProcurementCopilotPage";

const api = vi.hoisted(() => ({
  getAgentCatalog: vi.fn(),
  listModels: vi.fn(),
  streamCopilotMessage: vi.fn(),
  getAgentRunSteps: vi.fn(),
  listCopilotConversations: vi.fn(),
  getCopilotConversation: vi.fn(),
  deleteCopilotConversation: vi.fn(),
  listDataSources: vi.fn(),
  getPendingDeliverySummary: vi.fn(),
  searchMappings: vi.fn(),
  listPendingDeliverySites: vi.fn(),
}));

vi.mock("../../api/procurementKnowledge", () => ({
  getAgentCatalog: api.getAgentCatalog,
}));
vi.mock("../../api/modelConfig", () => ({ listModels: api.listModels }));
vi.mock("../../api/datasource", () => ({ listDataSources: api.listDataSources }));
vi.mock("../../api/entityMapping", () => ({ searchMappings: api.searchMappings }));
vi.mock("../../api/procurementCopilot", () => ({
  streamCopilotMessage: api.streamCopilotMessage,
  listCopilotConversations: api.listCopilotConversations,
  getCopilotConversation: api.getCopilotConversation,
  deleteCopilotConversation: api.deleteCopilotConversation,
  getPendingDeliverySummary: api.getPendingDeliverySummary,
  listPendingDeliverySites: api.listPendingDeliverySites,
}));
vi.mock("../../api/agentOrchestrationApi", () => ({
  getAgentRunSteps: api.getAgentRunSteps,
}));

beforeEach(() => {
  api.listCopilotConversations.mockResolvedValue([]);
  api.getPendingDeliverySummary.mockResolvedValue({
    summary: { openLines: 9828, openQty: 209421924.058, overdueLines: 9828, excludedRows: 7 },
    bySupplier: [{ supplierCode: "J1", openLines: 2405, openQty: 57363604, overdueLines: 2405 }],
    topLines: [],
  });
  api.listPendingDeliverySites.mockResolvedValue([
    { code: "T0", name: "泰鸿万立" },
    { code: "T3", name: "模具厂" },
  ]);
  api.searchMappings.mockResolvedValue([]);
  api.listDataSources.mockResolvedValue([
    { id: 1, name: "THBI Oracle", type: "oracle", isDefault: true },
    { id: 10, name: "SRM-TH-01", type: "mysql", isDefault: false },
  ]);
  api.getCopilotConversation.mockReset();
  api.deleteCopilotConversation.mockReset();
  api.deleteCopilotConversation.mockResolvedValue(undefined);
  localStorage.clear();
});

function renderPage() {
  const utils = render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <ProcurementCopilotPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
  // 首页是场景卡片墙：进「自由问答」视图（若本地留存已触发自动恢复则已在对话面）
  const freeCard = screen.queryByText(/自由问答/);
  if (freeCard) fireEvent.click(freeCard);
  return utils;
}

/** 进入「待到货」场景视图。 */
function renderScenario() {
  const utils = render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <ProcurementCopilotPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
  fireEvent.click(screen.getByText(/待到货/));
  return utils;
}

function mockOptions() {
  api.getAgentCatalog.mockResolvedValue([
    { agentCode: "PROCUREMENT_SOURCING", agentName: "采购寻源 Agent", runnable: true, status: "active", description: null, tool: null },
    { agentCode: "PROCUREMENT_COPILOT", agentName: "采购 AI 副驾", runnable: false, status: "active", description: null, tool: null },
  ]);
  api.listModels.mockResolvedValue([
    { id: 3, modelName: "Qwen3.8-27B-4bit", provider: "local", isActive: true },
  ]);
}

describe("ProcurementCopilotPage", () => {
  it("只列出可运行场景，并渲染执行轨迹与答案", async () => {
    mockOptions();
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      handlers.onRun?.({ runId: "r-1", stepsUrl: "/api/v1/agent-runs/r-1/steps" });
      handlers.onStep?.({
        stepNo: 0,
        iterationNo: 1,
        kind: "PLAN",
        status: "SUCCEEDED",
        action: "CALL_TOOL",
        planSummary: "先定位物料",
        toolName: "material_search",
        answer: null,
        errorCode: null,
        errorMessage: null,
      });
      handlers.onDone?.({
        runId: "r-1",
        status: "SUCCEEDED",
        answer: "命中 16 个物料",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 4337,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-1/steps",
      });
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(screen.getByText("命中 16 个物料")).toBeTruthy());
    // 步骤行不再进会话流（收进「查看执行轨迹」抽屉），答案为 Markdown 卡
    expect(screen.getByText("解读与建议")).toBeTruthy();
    expect(screen.getByText(/4337/)).toBeTruthy();
    // 不可运行的场景不在下拉里
    expect(screen.queryByText("采购 AI 副驾")).toBeNull();
  });

  it("PAUSED 渲染为追问（不是失败）", async () => {
    mockOptions();
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      handlers.onStep?.({
        stepNo: 2,
        iterationNo: 2,
        kind: "PLAN",
        status: "SKIPPED",
        action: "ASK_USER",
        planSummary: "请确认是哪一个规格的不锈钢板",
        toolName: null,
        answer: null,
        errorCode: null,
        errorMessage: null,
      });
      handlers.onDone?.({
        runId: "r-2",
        status: "PAUSED",
        answer: null,
        pauseCode: "NEEDS_INPUT",
        errorCode: null,
        errorMessage: null,
        totalTokens: 100,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-2/steps",
      });
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    // 追问会同时出现在轨迹（步骤）和高亮 Alert 里 —— 至少一处即可
    await waitFor(() =>
      expect(screen.getAllByText("请确认是哪一个规格的不锈钢板").length).toBeGreaterThan(0),
    );
    expect(screen.getByText("NEEDS_INPUT")).toBeTruthy();
  });

  it("发送失败展示后端 detail", async () => {
    mockOptions();
    api.streamCopilotMessage.mockRejectedValue(new Error("Copilot 只接受采购域 Agent"));

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() =>
      expect(screen.getByText("Copilot 只接受采购域 Agent")).toBeTruthy(),
    );
  });
});


describe("ProcurementCopilotPage 执行轨迹", () => {
  function mockOneTurn() {
    mockOptions();
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      handlers.onRun?.({ runId: "r-9", stepsUrl: "/api/v1/agent-runs/r-9/steps" });
      handlers.onDone?.({
        runId: "r-9",
        status: "SUCCEEDED",
        answer: "答案",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 10,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-9/steps",
      });
    });
  }

  async function sendOneQuestion() {
    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(screen.getByText("答案")).toBeTruthy());
  }

  it("轨迹按钮不是裸链接（裸链接带不上 Authorization ⇒ 新窗口只会看到「请先登录」）", async () => {
    mockOneTurn();
    await sendOneQuestion();

    expect(
      screen.queryByRole("link", { name: /查看执行轨迹/ }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: /查看执行轨迹/ }),
    ).toBeTruthy();
  });

  it("点击后带鉴权拉取 /agent-runs/{runId}/steps 并在页内抽屉展示", async () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    mockOneTurn();
    api.getAgentRunSteps.mockResolvedValue([
      {
        stepNo: 0,
        iterationNo: 1,
        kind: "PLAN",
        status: "SUCCEEDED",
        action: "CALL_TOOL",
        planSummary: "先定位物料",
        toolName: "material_search",
        inputJson: null,
        outputJson: null,
        answer: null,
        errorCode: null,
        errorMessage: null,
        promptTokens: 12,
        completionTokens: 3,
        cost: 0,
        startedAt: "2026-11-23T10:00:00Z",
        finishedAt: "2026-11-23T10:00:01Z",
      },
    ]);
    await sendOneQuestion();

    fireEvent.click(screen.getByRole("button", { name: /查看执行轨迹/ }));

    await waitFor(() =>
      expect(api.getAgentRunSteps).toHaveBeenCalledWith("r-9"),
    );
    await waitFor(() =>
      expect(screen.getAllByText("先定位物料").length).toBeGreaterThan(0),
    );
    // 不新开窗口（否则必然回到「请先登录」）
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it("先收到超时事件、随后传输中断：保留超时文案（不被网络错误覆盖）", async () => {
    mockOptions();
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      handlers.onRun?.({ runId: "r-7", stepsUrl: "/api/v1/agent-runs/r-7/steps" });
      handlers.onError?.({
        error: "本次流式转发的等待时间已到；运行仍在后台继续，可用轨迹地址继续跟进",
        errorType: "timeout",
        runId: "r-7",
        stepsUrl: "/api/v1/agent-runs/r-7/steps",
      });
      // nginx 在上游正常收尾后仍可能重置连接 ⇒ fetch 的 body reader 抛 TypeError
      throw new TypeError("Failed to fetch");
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() =>
      expect(screen.getByText(/流式转发的等待时间已到/)).toBeTruthy(),
    );
    expect(screen.queryByText("Failed to fetch")).toBeNull();
  });

  it("拉取失败时在抽屉内提示错误，而不是空白", async () => {
    mockOneTurn();
    api.getAgentRunSteps.mockRejectedValue(new Error("请先登录"));
    await sendOneQuestion();

    fireEvent.click(screen.getByRole("button", { name: /查看执行轨迹/ }));

    await waitFor(() => expect(screen.getByText("请先登录")).toBeTruthy());
  });
});

describe("ProcurementCopilotPage 多轮会话", () => {
  it("第二轮带上后端回传的 conversationId；新会话清空会话 id", async () => {
    mockOptions();
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onRun?.({
        runId: `r-${payloads.length}`,
        stepsUrl: "/api/v1/agent-runs/x/steps",
        conversationId: "conv-123456",
        resumed: false,
      });
      handlers.onDone?.({
        runId: `r-${payloads.length}`,
        status: "SUCCEEDED",
        answer: "答案",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 1,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/x/steps",
      });
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());

    const box = screen.getByPlaceholderText(/不锈钢板/);
    fireEvent.change(box, { target: { value: "第一问" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(payloads).toHaveLength(1));
    expect(payloads[0].conversationId).toBeUndefined();

    fireEvent.change(box, { target: { value: "第二问" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(payloads).toHaveLength(2));
    expect(payloads[1].conversationId).toBe("conv-123456");

    fireEvent.click(screen.getByRole("button", { name: /新会话/ }));
    fireEvent.change(box, { target: { value: "第三问" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(payloads).toHaveLength(3));
    expect(payloads[2].conversationId).toBeUndefined();
  });
});

describe("ProcurementCopilotPage 历史会话", () => {
  it("刷新后按本地留存恢复上一次会话（问题 + 答案）", async () => {
    mockOptions();
    localStorage.setItem("copilot.conversationId", "c-1");
    api.listCopilotConversations.mockResolvedValue([
      {
        conversationId: "c-1",
        agentCode: "PROCUREMENT_SOURCING",
        modelConfigId: 3,
        turnCount: 1,
        lastQuestion: "历史问题",
        updatedAt: "2026-11-23T10:00:00Z",
      },
    ]);
    api.getCopilotConversation.mockResolvedValue({
      conversationId: "c-1",
      agentCode: "PROCUREMENT_SOURCING",
      modelConfigId: 3,
      turns: [
        {
          seq: 1,
          question: "历史问题",
          runId: "r-old",
          status: "SUCCEEDED",
          answer: "历史答案",
          pauseCode: null,
          createdAt: "2026-11-23T10:00:00Z",
        },
      ],
    });

    renderPage();

    await waitFor(() =>
      expect(api.getCopilotConversation).toHaveBeenCalledWith("c-1"),
    );
    await waitFor(() =>
      expect(screen.getAllByText("历史问题").length).toBeGreaterThan(0),
    );
    expect(screen.getByText("历史答案")).toBeTruthy();
    // 恢复的轮次也能看轨迹（runId 来自轮次记录）
    expect(screen.getByRole("button", { name: /查看执行轨迹/ })).toBeTruthy();
  });

  it("首次对话拿到会话 id 后落盘，供下次刷新恢复", async () => {
    mockOptions();
    localStorage.removeItem("copilot.conversationId");
    api.listCopilotConversations.mockResolvedValue([]);
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      handlers.onRun?.({
        runId: "r-2",
        stepsUrl: "/api/v1/agent-runs/r-2/steps",
        conversationId: "c-2",
      });
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() =>
      expect(localStorage.getItem("copilot.conversationId")).toBe("c-2"),
    );
  });
});

describe("ProcurementCopilotPage 历史侧栏", () => {
  it("点击侧栏会话即恢复该会话（不需要下拉框）", async () => {
    mockOptions();
    api.listCopilotConversations.mockResolvedValue([
      {
        conversationId: "c-a",
        agentCode: "PROCUREMENT_SOURCING",
        modelConfigId: 3,
        turnCount: 2,
        lastQuestion: "会话A的问题",
        updatedAt: "2026-11-23T10:00:00Z",
      },
    ]);
    api.getCopilotConversation.mockResolvedValue({
      conversationId: "c-a",
      agentCode: "PROCUREMENT_SOURCING",
      modelConfigId: 3,
      turns: [
        {
          seq: 1,
          question: "会话A的问题",
          runId: "r-a",
          status: "SUCCEEDED",
          answer: "会话A的答案",
          pauseCode: null,
          createdAt: "2026-11-23T10:00:00Z",
        },
      ],
    });

    renderPage();
    await waitFor(() => expect(screen.getByText("会话A的问题")).toBeTruthy());

    fireEvent.click(screen.getByText("会话A的问题"));

    await waitFor(() =>
      expect(api.getCopilotConversation).toHaveBeenCalledWith("c-a"),
    );
    await waitFor(() => expect(screen.getByText("会话A的答案")).toBeTruthy());
  });
});

describe("ProcurementCopilotPage 删除会话", () => {
  it("侧栏删除：Popconfirm 确认后调接口并刷新列表", async () => {
    mockOptions();
    api.listCopilotConversations.mockResolvedValue([
      {
        conversationId: "c-del",
        agentCode: "PROCUREMENT_SOURCING",
        modelConfigId: 3,
        turnCount: 1,
        lastQuestion: "要删的问题",
        updatedAt: "2026-11-23T10:00:00Z",
      },
    ]);

    renderPage();
    await waitFor(() => expect(screen.getByText("要删的问题")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /删除会话/ }));
    await waitFor(() =>
      expect(screen.getByText(/确定删除该会话吗/)).toBeTruthy(),
    );

    const okButtons = screen.getAllByRole("button", { name: /确\s?定/ });
    fireEvent.click(okButtons[okButtons.length - 1]);

    await waitFor(() =>
      expect(api.deleteCopilotConversation).toHaveBeenCalledWith("c-del"),
    );
    await waitFor(() =>
      expect(api.listCopilotConversations.mock.calls.length).toBeGreaterThan(1),
    );
  });
});


describe("ProcurementCopilotPage 待到货场景", () => {
  it("场景卡：默认槽位运行 ⇒ 发出的消息带 pending_delivery_query 与时间窗", async () => {
    mockOptions();
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onDone?.({
        runId: "r-s1",
        status: "SUCCEEDED",
        answer: "欠交汇总",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 1,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-s1/steps",
      });
    });
    // done 后回读终态步骤：TOOL 带 outputJson ⇒ 结构化卡渲染
    api.getAgentRunSteps.mockResolvedValue([
      {
        stepNo: 1, iterationNo: 1, kind: "TOOL", status: "SUCCEEDED",
        action: null, planSummary: null, toolName: "pending_delivery_query",
        inputJson: null,
        outputJson: { text: JSON.stringify({ data: {
          summary: { openLines: 9828, openQty: 209421924.058, overdueLines: 9828, excludedRows: 7 },
          bySupplier: [{ supplierCode: "J1", openLines: 2405, openQty: 57363604, overdueLines: 2405 }],
          topLines: [{ poNo: "T22511POH1251Q", poLine: 1000, materialCode: "502447700001",
            supplierCode: "F109", orderedQty: 1000000, receivedQty: 9600, openQty: 990400,
            expectedDate: "2025-11-28 00:00:00", overdueDays: 360 }],
        }, answer: "" }) },
        answer: null, errorCode: null, errorMessage: null,
        promptTokens: 0, completionTokens: 0, cost: 0,
        startedAt: "2026-11-23T10:00:00Z", finishedAt: "2026-11-23T10:00:01Z",
      },
    ]);

    renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByText("全部供应商概览（近 12 个月）"));

    await waitFor(() => expect(payloads).toHaveLength(1));
    const message = String(payloads[0].message);
    expect(message).toContain("pending_delivery_query");
    expect(message).toContain("近12个月");
    expect(message).toContain("供应商=全部");
    expect(message).toContain("最低逾期天数=0");
    // 数据源槽位：默认选中 isDefault 源，消息里带 id 给工具取
    expect(message).toContain("数据源 id=1");
    expect(message).toContain("datasourceId=1");
    // 场景钉死 Agent：即使页面默认选了别的 Agent，也要以 SOURCING 发起
    expect(payloads[0].agentCode).toBe("PROCUREMENT_SOURCING");
    await waitFor(() => expect(screen.getByText("欠交汇总")).toBeTruthy());
    // done 回读步骤 ⇒ 结构化结果卡（数据不经 LLM 复述）
    await waitFor(() =>
      expect(screen.getByText("待到货分析结果")).toBeTruthy(),
    );
    expect(screen.getByText("J1")).toBeTruthy();
    expect(screen.getByText("T22511POH1251Q")).toBeTruthy();
  });

  it("高级参数：基地多选后消息带 siteKeys 与用户文字", async () => {
    mockOptions();
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onDone?.({
        runId: "r-s2",
        status: "SUCCEEDED",
        answer: "ok",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 1,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-s2/steps",
      });
    });

    renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));

    // 基地多选（本地选项，label = 名称（编码））
    const siteSelect = screen.getAllByRole("combobox")[4];
    fireEvent.mouseDown(siteSelect);
    fireEvent.click(await waitFor(() => screen.getByText("模具厂（T3）")));

    fireEvent.change(screen.getByPlaceholderText(/补充说明/), {
      target: { value: "看 T3 的欠交" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(payloads).toHaveLength(1));
    const message = String(payloads[0].message);
    expect(message).toContain("基地=T3");
    expect(message).toContain("siteKeys=[T3");
    expect(message).toContain("看 T3 的欠交");
    expect(message).toContain("pending_delivery_query");
  });
});


describe("ProcurementCopilotPage 场景钉 Agent", () => {
  it("当前会话绑的是合同 Agent 时，场景运行自动换 SOURCING 并开新会话（清 conversationId）", async () => {
    // 让默认选中的场景是合同 Agent（catalog 第一个 runnable），
    // 这样第一问绑定 CONTRACT 会话，场景运行时才会触发「换 Agent ⇒ 新会话」。
    api.getAgentCatalog.mockResolvedValue([
      { agentCode: "PROCUREMENT_CONTRACT_SEARCH_AGENT", agentName: "合同检索 Agent", runnable: true, status: "active", description: null, tool: null },
      { agentCode: "PROCUREMENT_SOURCING", agentName: "采购寻源 Agent", runnable: true, status: "active", description: null, tool: null },
    ]);
    api.listModels.mockResolvedValue([
      { id: 3, modelName: "Qwen3.8-27B-4bit", provider: "local", isActive: true },
    ]);
    // 先手动走一轮拿到会话 id（绑定到默认选中的场景）
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onRun?.({
        runId: `r-${payloads.length}`,
        stepsUrl: "/api/v1/agent-runs/x/steps",
        conversationId: `conv-${payloads.length}`,
      });
      handlers.onDone?.({
        runId: `r-${payloads.length}`,
        status: "SUCCEEDED",
        answer: "ok",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 1,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/x/steps",
      });
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());

    // 第一问：自由问答视图（默认 catalog 第一个 runnable）
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "自由问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(payloads).toHaveLength(1));

    // 场景运行：先回卡片墙进「待到货」，点 chip —— Agent 钉 SOURCING 且不带会话 id（防 422 串味）
    fireEvent.click(screen.getAllByRole("button", { name: /场景$/ })
      .find((el) => el.textContent?.trim() === "场景") as HTMLElement);
    fireEvent.click(screen.getByText("待到货 / 逾期未到"));
    await waitFor(() =>
      expect(screen.getByText("全部供应商概览（近 12 个月）")).toBeTruthy(),
    );
    fireEvent.click(screen.getByText("全部供应商概览（近 12 个月）"));
    await waitFor(() => expect(payloads).toHaveLength(2));
    expect(payloads[1].agentCode).toBe("PROCUREMENT_SOURCING");
    expect(payloads[1].conversationId).toBeUndefined();
  });
});


describe("ProcurementCopilotPage 场景卡片墙（首页）", () => {
  it("首页渲染场景大卡（待到货 + 自由问答），点进入才见工作区", async () => {
    mockOptions();
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <ConfigProvider locale={zhCN}>
          <ProcurementCopilotPage />
        </ConfigProvider>
      </I18nextProvider>,
    );
    expect(screen.getByText("待到货 / 逾期未到")).toBeTruthy();
    expect(screen.getByText(/自由问答/)).toBeTruthy();
    // 首页不显示对话输入框
    expect(screen.queryByPlaceholderText(/不锈钢板/)).toBeNull();

    // 进入待到货：欢迎态（问候 + 业务现状 + 推荐 chips）
    fireEvent.click(screen.getByText("待到货 / 逾期未到"));
    await waitFor(() => expect(screen.getByText(/你好，我是采购待到货分析助手/)).toBeTruthy());
    expect(screen.getByText("业务现状（实时）")).toBeTruthy();
    expect(screen.getByText("试试这样问我")).toBeTruthy();
    expect(container).toBeTruthy();

    // 返回卡片墙
    const backBtn = screen.getAllByRole("button", { name: /场景/ })
      .find((el) => el.textContent?.trim() === "场景");
    fireEvent.click(backBtn as HTMLElement);
    expect(screen.getByText("待到货 / 逾期未到")).toBeTruthy();
  });
});


describe("ProcurementCopilotPage 数据源槽位", () => {
  it("切换数据源后，场景消息带新数据源 id", async () => {
    mockOptions();
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onDone?.({
        runId: "r-ds",
        status: "SUCCEEDED",
        answer: "ok",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 1,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-ds/steps",
      });
    });

    renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));
    // 高级面板里 Select 顺序：模型(0)、数据源(1)、供应商(2)、物料(3)、基地(4)
    const selects = screen.getAllByRole("combobox");
    fireEvent.mouseDown(selects[1]);
    fireEvent.click(await waitFor(() => screen.getByText("SRM-TH-01")));

    fireEvent.change(screen.getByPlaceholderText(/补充说明/), {
      target: { value: "看欠交" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(payloads).toHaveLength(1));
    const message = String(payloads[0].message);
    expect(message).toContain("数据源 id=10");
    expect(message).toContain("datasourceId=10");
  });
});


describe("供应商/物料远程搜索多选", () => {
  it("供应商搜索命中后可多选，消息带 supplierKeys", async () => {
    mockOptions();
    api.searchMappings.mockResolvedValue([
      {
        id: 1, entityType: "SUPPLIER", enterpriseKey: 1,
        enterpriseCode: "F109", sourceSystem: "ERP", sourceCode: "ERP-1",
        name: "飞驰机械",
      },
    ]);
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onDone?.({
        runId: "r-sup",
        status: "SUCCEEDED",
        answer: "ok",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 1,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-sup/steps",
      });
    });

    renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));

    // 供应商多选（combobox 顺序：模型0/数据源1/供应商2）
    const supplierSelect = screen.getAllByRole("combobox")[2];
    fireEvent.mouseDown(supplierSelect);
    fireEvent.change(supplierSelect, { target: { value: "飞驰" } });
    // 300ms debounce ⇒ 等待远程搜索回来
    await waitFor(() => expect(api.searchMappings).toHaveBeenCalled(), { timeout: 2000 });
    fireEvent.click(await waitFor(() => screen.getByText("飞驰机械（F109）")));

    fireEvent.change(screen.getByPlaceholderText(/补充说明/), {
      target: { value: "看供应商欠交" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(payloads).toHaveLength(1));
    const message = String(payloads[0].message);
    expect(message).toContain("供应商=F109");
    expect(message).toContain("supplierKeys=[F109");
  });
});
