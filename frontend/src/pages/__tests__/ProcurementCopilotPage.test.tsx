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
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import i18n from "../../i18n/i18n";
import type { CopilotScenario } from "../../api/copilotScenario";
import ProcurementCopilotPage, {
  buildChipSlots,
  defaultSlotValues,
} from "../ProcurementCopilotPage";

const authState = vi.hoisted(() => ({ roles: ["admin"] as string[] }));

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
  getDeliveryTrackingOverview: vi.fn(),
  searchMappings: vi.fn(),
  listPendingDeliverySites: vi.fn(),
  listCopilotScenarios: vi.fn(),
}));

/** P4 场景配置（与 backend/scripts/seed_copilot_scenarios.py 同构）：
 * 卡片墙 / 槽位表单 / chips 全部由此驱动；提问文本由后端渲染。 */
const SCENARIO_CONFIG = {
  scenarioCode: "pending_delivery",
  name: "待到货 / 逾期未到",
  description: "按供应商汇总欠交量并给催货建议，引用订单号与物料编码",
  iconCode: "car",
  agentCode: "PROCUREMENT_SOURCING",
  slotSchema: [
    { key: "monthsBack", type: "number", label: "时间窗", required: true,
      conflictKeywords: ["时间窗"], displayTemplate: "近{value}个月" },
    { key: "supplierKeys", type: "entityMulti", label: "供应商",
      conflictKeywords: ["供应商", "厂家"],
      codePattern: "(?<![0-9A-Za-z])[A-Z]\\d{2,4}(?![0-9A-Za-z])" },
    { key: "materialKeys", type: "entityMulti", label: "物料",
      conflictKeywords: ["物料", "材料"],
      codePattern: "(?<![0-9A-Za-z])(?:[A-Z]{2,}\\d{4,}[A-Z]?|\\d{10,}[A-Z]?)(?![0-9A-Za-z])" },
    { key: "siteKeys", type: "dictMulti", label: "基地",
      conflictKeywords: ["基地", "厂区", "工厂", "车间"],
      codePattern: "(?<![0-9A-Za-z])(?:T\\d{1,2}|C\\d)(?![0-9A-Za-z])" },
    { key: "minOverdueDays", type: "number", label: "最低逾期天数" },
    { key: "dateFrom", type: "text", label: "开始日期",
      conflictKeywords: ["日期", "时间段"], hidden: true },
    { key: "dateTo", type: "text", label: "结束日期",
      conflictKeywords: ["日期", "时间段"], hidden: true },
  ],
  questionTemplate: "分析待到货情况",
  chips: [
    { label: "全部供应商概览（近 12 个月）", monthsBack: 12, minOverdueDays: 0 },
    { label: "近期欠交（近 3 个月）", monthsBack: 3, minOverdueDays: 0 },
  ],
  resultRenderer: "pending_delivery",
  requiredRole: null,
};

vi.mock("../../api/procurementKnowledge", () => ({
  getAgentCatalog: api.getAgentCatalog,
}));
vi.mock("../../api/modelConfig", () => ({ listModels: api.listModels }));
vi.mock("../../api/datasource", () => ({ listDataSources: api.listDataSources }));
vi.mock("../../stores/authStore", () => ({
  useAuthStore: (selector: (state: { user: unknown }) => unknown) =>
    selector({ user: { roles: authState.roles } }),
}));
vi.mock("../../api/entityMapping", () => ({ searchMappings: api.searchMappings }));
vi.mock("../../api/procurementCopilot", () => ({
  streamCopilotMessage: api.streamCopilotMessage,
  listCopilotConversations: api.listCopilotConversations,
  getCopilotConversation: api.getCopilotConversation,
  deleteCopilotConversation: api.deleteCopilotConversation,
  getPendingDeliverySummary: api.getPendingDeliverySummary,
  getDeliveryTrackingOverview: api.getDeliveryTrackingOverview,
  listPendingDeliverySites: api.listPendingDeliverySites,
}));
vi.mock("../../api/agentOrchestrationApi", () => ({
  getAgentRunSteps: api.getAgentRunSteps,
}));
vi.mock("../../api/copilotScenario", () => ({
  listCopilotScenarios: api.listCopilotScenarios,
}));

beforeEach(() => {
  api.listCopilotConversations.mockResolvedValue([]);
  api.listCopilotScenarios.mockResolvedValue([SCENARIO_CONFIG]);
  api.getPendingDeliverySummary.mockResolvedValue({
    summary: { openLines: 9828, openQty: 209421924.058, overdueLines: 9828, excludedRows: 7 },
    bySupplier: [{ supplierCode: "J1", openLines: 2405, openQty: 57363604, overdueLines: 2405 }],
    topLines: [],
  });
  api.listPendingDeliverySites.mockResolvedValue([
    { code: "T0", name: "泰鸿万立" },
    { code: "T3", name: "模具厂" },
  ]);
  api.getDeliveryTrackingOverview.mockResolvedValue({
    window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
    summary: {
      totalLines: 19902, totalOrders: 4843,
      orderedQty: 88619265.255, receivedQty: 41414210.255,
      orderArrivalRate: 83.67, qtyArrivalRate: 46.73,
      allArrivedOrders: 4052, partialArrivedOrders: 434,
      notArrivedOrders: 357, excludedRows: 6,
    },
    bySupplier: [{
      supplierCode: "B125", supplierName: "浙江力航汽车部件有限公司",
      totalOrders: 29, allArrivedOrders: 1, partialArrivedOrders: 16,
      notArrivedOrders: 12, orderArrivalRate: 3.45,
      orderedQty: 15155100, receivedQty: 2176026,
    }],
  });
  api.searchMappings.mockResolvedValue([]);
  api.listDataSources.mockResolvedValue([
    { id: 1, name: "THBI Oracle", type: "oracle", isDefault: true },
    { id: 10, name: "SRM-TH-01", type: "mysql", isDefault: false },
  ]);
  api.getAgentRunSteps.mockReset();
  authState.roles = ["admin"];
  api.getCopilotConversation.mockReset();
  api.deleteCopilotConversation.mockReset();
  api.deleteCopilotConversation.mockResolvedValue(undefined);
  localStorage.clear();
});

function renderPage() {
  const utils = render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <ConfigProvider locale={zhCN}>
          <ProcurementCopilotPage />
        </ConfigProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
  // 首页是场景卡片墙：进「自由问答」视图（若本地留存已触发自动恢复则已在对话面）
  const freeCard = screen.queryByText(/自由问答/);
  if (freeCard) fireEvent.click(freeCard);
  return utils;
}

/** 进入「待到货」场景视图（卡片墙是配置驱动 ⇒ 先等场景列表到位）。 */
async function renderScenario() {
  const utils = render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <ConfigProvider locale={zhCN}>
          <ProcurementCopilotPage />
        </ConfigProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
  const card = await waitFor(() => screen.getByText(/待到货/));
  fireEvent.click(card);
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
    // 轮次抽屉默认收起：测试需要内部按钮时先展开
    fireEvent.click(screen.getAllByText("分析内容")[0]);
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

    fireEvent.click(await waitFor(() => screen.findByRole("button", { name: /查看执行轨迹/ })));

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
  it("刷新后停在场景卡片墙，不自动恢复旧会话（修复跨账号 404 与串台）", async () => {
    mockOptions();
    // 模拟旧版本留下的 storage 记录（可能属于其他账号）
    localStorage.setItem("copilot.conversationId", "c-stale");
    api.getCopilotConversation.mockResolvedValue({
      conversationId: "c-stale",
      agentCode: "PROCUREMENT_SOURCING",
      modelConfigId: 3,
      turns: [],
    });

    render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <ConfigProvider locale={zhCN}>
            <ProcurementCopilotPage />
          </ConfigProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );

    // 落在卡片墙，且**没有**去拉旧会话详情（404 噪音消除）
    await waitFor(() => expect(screen.getByText(/自由问答/)).toBeTruthy());
    expect(api.getCopilotConversation).not.toHaveBeenCalled();
  });

  it("首轮拿到会话 id 不再落盘（历史统一走侧栏恢复）", async () => {
    mockOptions();
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

    await waitFor(() => expect(screen.getByText("RUNNING")).toBeTruthy());
    expect(localStorage.getItem("copilot.conversationId")).toBeNull();
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
    // 侧栏默认收起：点右侧浮钮展开
    fireEvent.click(screen.getByRole("button", { name: /展开/ }));
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
    // 侧栏默认收起：点右侧浮钮展开
    fireEvent.click(screen.getByRole("button", { name: /展开/ }));
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
          bySupplier: [{ supplierCode: "J1", supplierName: "河北新泰鸿汽车零部件有限公司",
            openLines: 2405, openQty: 57363604, overdueLines: 2405 }],
          topLines: [{ poNo: "T22511POH1251Q", poLine: 1000, materialCode: "502447700001",
            supplierCode: "F109", supplierName: "上海纳特汽车标准件有限公司",
            orderedQty: 1000000, receivedQty: 9600, openQty: 990400,
            expectedDate: "2025-11-28 00:00:00", overdueDays: 360 }],
        }, answer: "" }) },
        answer: null, errorCode: null, errorMessage: null,
        promptTokens: 0, completionTokens: 0, cost: 0,
        startedAt: "2026-11-23T10:00:00Z", finishedAt: "2026-11-23T10:00:01Z",
      },
    ]);

    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByText("全部供应商概览（近 12 个月）"));

    await waitFor(() => expect(payloads).toHaveLength(1));
    // P4 新契约：前端只上送场景编码 + 结构化槽位；提问文本由**后端**渲染
    //（pending_delivery_query / 近12个月 / 数据源 等口径断言已移至后端渲染单测）
    expect(payloads[0].scenarioCode).toBe("pending_delivery");
    // 场景钉死 Agent：即使页面默认选了别的 Agent，也要以 SOURCING 发起
    expect(payloads[0].agentCode).toBe("PROCUREMENT_SOURCING");
    expect(String(payloads[0].message)).toBe("");
    const slots = payloads[0].slots as Record<string, unknown>;
    expect(slots.monthsBack).toBe(12);
    expect(slots.minOverdueDays).toBe(0);
    expect(slots.supplierKeys).toEqual([]);
    // THBI 硬规则：admin 也不再随槽位上送数据源
    expect("datasourceId" in slots).toBe(false);
    await waitFor(() => expect(screen.getByText("欠交汇总")).toBeTruthy());
    // done 回读步骤 ⇒ 结构化结果卡（数据不经 LLM 复述）
    await waitFor(() =>
      expect(screen.getByText("待到货分析结果")).toBeTruthy(),
    );
    // 概览面板默认展开（J1 在供应商汇总面板，收起态不渲染）
    await waitFor(() =>
      expect(screen.getAllByText("9,828").length).toBeGreaterThan(0),
    );
    // Collapse 头节点类名定位（文本匹配会命中祖先节点 ⇒ 不用文本找）。
    // 结果卡的 suppliers/lines 是后两个面板头（前面可能是欢迎卡遗留的头）。
    const headers = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-collapse-header"),
    ).filter((el) => el.textContent?.includes("按供应商汇总") || el.textContent?.includes("TOP 欠交订单行"));
    expect(headers.length).toBe(2);
    fireEvent.click(headers[0]);   // 展开供应商汇总
    fireEvent.click(headers[1]);   // 展开 TOP 行
    // 名称出现在：欢迎态统计卡 TOP 文案 + bySupplier 表（多处）⇒ getAllByText
    await waitFor(() =>
      expect(screen.getAllByText(/河北新泰鸿汽车零部件有限公司/).length).toBeGreaterThan(0),
    );
    expect(screen.getByText("T22511POH1251Q")).toBeTruthy();
  });

  it("高级参数：文字含基地码（T3）⇒ 基地槽位让位（参数与文字冲突以文字为准）", async () => {
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

    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));

    // 基地多选（本地选项，label = 名称（编码））
    const siteSelect = screen.getAllByRole("combobox")[3];
    fireEvent.mouseDown(siteSelect);
    fireEvent.click(await waitFor(() => screen.getByText("模具厂（T3）")));

    fireEvent.change(screen.getByPlaceholderText(/补充说明/), {
      target: { value: "看 T3 的欠交" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(payloads).toHaveLength(1));
    // P4 新契约：前端透传槽位与文字；「让位」判定移至后端渲染器
    //（尾注/入参的让位断言在后端 test_copilot_scenario_render.py）
    const slots = payloads[0].slots as Record<string, unknown>;
    expect(slots.siteKeys).toEqual(["T3"]);
    expect(String(payloads[0].message)).toContain("看 T3 的欠交");
    expect(payloads[0].scenarioCode).toBe("pending_delivery");
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
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <ConfigProvider locale={zhCN}>
            <ProcurementCopilotPage />
          </ConfigProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );
    // 卡片来自场景配置（异步加载）⇒ 等「待到货」卡出现再断言
    await waitFor(() => expect(screen.getByText("待到货 / 逾期未到")).toBeTruthy());
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
    await waitFor(() => expect(screen.getByText("待到货 / 逾期未到")).toBeTruthy());
  });
});


describe("ProcurementCopilotPage 数据源不再随槽位上送", () => {
  it("admin：场景消息 slots 不含 datasourceId（THBI 硬规则）", async () => {
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

    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText(/补充说明/), {
      target: { value: "看欠交" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(payloads).toHaveLength(1));
    const slots = payloads[0].slots as Record<string, unknown>;
    expect("datasourceId" in slots).toBe(false);
    expect(payloads[0].scenarioCode).toBe("pending_delivery");
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

    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));

    // 供应商多选（combobox 顺序：模型0/供应商1）
    const supplierSelect = screen.getAllByRole("combobox")[1];
    fireEvent.mouseDown(supplierSelect);
    fireEvent.change(supplierSelect, { target: { value: "飞驰" } });
    // 300ms debounce ⇒ 等待远程搜索回来
    await waitFor(() => expect(api.searchMappings).toHaveBeenCalled(), { timeout: 2000 });
    fireEvent.click(await waitFor(() => screen.getByText("飞驰机械（F109）")));

    fireEvent.change(screen.getByPlaceholderText(/补充说明/), {
      target: { value: "看一下欠交明细" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(payloads).toHaveLength(1));
    const slots = payloads[0].slots as Record<string, unknown>;
    expect(slots.supplierKeys).toEqual(["F109"]);
    expect(String(payloads[0].message)).toContain("看一下欠交明细");
  });
});


function twoResultSteps(poNo: string) {
  return [
    {
      stepNo: 1, iterationNo: 1, kind: "TOOL", status: "SUCCEEDED",
      action: null, planSummary: null, toolName: "pending_delivery_query",
      inputJson: null,
      outputJson: { text: JSON.stringify({ data: {
        summary: { openLines: 1, openQty: 10, overdueLines: 1, excludedRows: 0 },
        bySupplier: [], topLines: [{ poNo, poLine: 1000, materialCode: "M1",
          supplierCode: "S1", orderedQty: 100, receivedQty: 90, openQty: 10,
          expectedDate: "2026-01-01", overdueDays: 5 }],
      }, answer: "" }) },
      answer: null, errorCode: null, errorMessage: null,
      promptTokens: 0, completionTokens: 0, cost: 0,
      startedAt: "2026-11-24T00:00:00Z", finishedAt: "2026-11-24T00:00:01Z",
    },
  ];

}

describe("三个交互优化", () => {

  it("③ 会话内多结果：Tabs 切换显示不同轮次的结果", async () => {
    mockOptions();
    let n = 0;
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      n += 1;
      const poNo = n === 1 ? "PO-AAA" : "PO-BBB";
      handlers.onRun?.({ runId: `r-${n}`, stepsUrl: "/api/v1/agent-runs/x/steps", conversationId: "c-1" });
      // 必须在 onDone **之前**设置：onDone 同步触发 refreshSteps，
      // 后置的 mockResolvedValue 赶不上这次调用（实测踩坑）
      api.getAgentRunSteps.mockResolvedValue(twoResultSteps(poNo));
      handlers.onDone?.({
        runId: `r-${n}`, status: "SUCCEEDED", answer: `答案${n}`,
        pauseCode: null, errorCode: null, errorMessage: null,
        totalTokens: 1, cost: 0, stepsUrl: "/api/v1/agent-runs/x/steps",
      });
    });

    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByText("全部供应商概览（近 12 个月）"));
    await waitFor(() => expect(screen.getByText("待到货分析结果")).toBeTruthy());
    // TOP 行面板收起态不渲染 ⇒ 展开后行数据可见
    fireEvent.click(screen.getByText(/TOP 欠交订单行/));
    await waitFor(() => expect(screen.getByText("PO-AAA")).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText(/补充说明/), {
      target: { value: "再按基地看一次" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(screen.getAllByText("待到货分析结果").length).toBeGreaterThan(0));
    fireEvent.click(screen.getByText(/TOP 欠交订单行/));
    await waitFor(() => expect(screen.getByText("PO-BBB")).toBeTruthy());
    // 两个 Tab；当前激活的是最近一次（#2），切回 #1 应显示 PO-AAA
    expect(screen.getByRole("tab", { name: /#1/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /#2/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /#1/ }));
    // 切换器只渲染激活项 ⇒ PO-BBB 离开 DOM；新激活的结果卡面板重新收起 ⇒ 展开 TOP
    fireEvent.click(screen.getByText(/TOP 欠交订单行/));
    await waitFor(() => expect(screen.getByText("PO-AAA")).toBeTruthy());
    expect(screen.queryByText("PO-BBB")).toBeNull();
  });

  it("② 仅改高级参数（不写字）发送按钮可用，消息用默认主句", async () => {
    mockOptions();
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onDone?.({
        runId: "r-adv", status: "SUCCEEDED", answer: "ok",
        pauseCode: null, errorCode: null, errorMessage: null,
        totalTokens: 1, cost: 0, stepsUrl: "/api/v1/agent-runs/x/steps",
      });
    });

    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));
    const siteSelect = screen.getAllByRole("combobox")[3];
    fireEvent.mouseDown(siteSelect);
    fireEvent.click(await waitFor(() => screen.getByText("模具厂（T3）")));

    const sendBtn = screen.getByRole("button", { name: /发\s*送/ });
    expect((sendBtn as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(sendBtn);

    await waitFor(() => expect(payloads).toHaveLength(1));
    // P4 新契约：无文字 ⇒ message 为空，主句由后端模板提供（「分析待到货情况」）
    expect(String(payloads[0].message)).toBe("");
    const slots = payloads[0].slots as Record<string, unknown>;
    expect(slots.siteKeys).toEqual(["T3"]);
    expect(slots.monthsBack).toBe(12);
  });

  it("① 重进场景：高级参数重置（summary 请求不再带 siteKeys）", async () => {
    mockOptions();
    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));
    const siteSelect = screen.getAllByRole("combobox")[3];
    fireEvent.mouseDown(siteSelect);
    fireEvent.click(await waitFor(() => screen.getByText("模具厂（T3）")));
    await waitFor(() => {
      const calls = api.getPendingDeliverySummary.mock.calls;
      expect(calls[calls.length - 1][0]?.siteKeys).toEqual(["T3"]);
    });

    // 回卡片墙再进入 ⇒ 参数重置
    fireEvent.click(screen.getAllByRole("button", { name: /场景$/ })
      .find((el) => el.textContent?.trim() === "场景") as HTMLElement);
    fireEvent.click(screen.getByText("待到货 / 逾期未到"));
    await waitFor(() => {
      const calls = api.getPendingDeliverySummary.mock.calls;
      const last = calls[calls.length - 1][0] ?? {};
      expect(last.siteKeys ?? []).toEqual([]);
    });
  });
});


describe("冲突参数以对话框为准", () => {
  it("高级参数选了 B125，对话框指定 B019 ⇒ 消息只带 B019（供应商槽位让位）", async () => {
    mockOptions();
    api.searchMappings.mockResolvedValue([
      {
        id: 1, entityType: "SUPPLIER", enterpriseKey: 1,
        enterpriseCode: "B125", sourceSystem: "ERP", sourceCode: "ERP-1",
        name: "B125供应商",
      },
    ]);
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onDone?.({
        runId: "r-c", status: "SUCCEEDED", answer: "ok",
        pauseCode: null, errorCode: null, errorMessage: null,
        totalTokens: 1, cost: 0, stepsUrl: "/api/v1/agent-runs/x/steps",
      });
    });

    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));
    // 与既有供应商搜索用例同款手法（mouseDown + change → debounce → click option）
    const supplierSelect = screen.getAllByRole("combobox")[1];
    fireEvent.mouseDown(supplierSelect);
    fireEvent.change(supplierSelect, { target: { value: "B125" } });
    await waitFor(() => expect(api.searchMappings).toHaveBeenCalled(), { timeout: 2000 });
    fireEvent.click(await waitFor(() => screen.getByText("B125供应商（B125）"), { timeout: 3000 }));

    fireEvent.change(screen.getByPlaceholderText(/补充说明/), {
      target: { value: "分析B019这个供应商的待到货情况" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(payloads).toHaveLength(1));
    // P4 新契约：前端透传 B125 槽位与 B019 文字；「文字提到供应商 ⇒
    // B125 让位」的判定在后端渲染器（test_copilot_scenario_render.py 覆盖）
    const slots = payloads[0].slots as Record<string, unknown>;
    expect(slots.supplierKeys).toEqual(["B125"]);
    expect(String(payloads[0].message)).toContain("B019");
  });
});


describe("冲突检测：纯编码也算指定", () => {
  it("对话框写「B109待到货」（无「供应商」关键字）⇒ 供应商槽位仍让位", async () => {
    mockOptions();
    api.searchMappings.mockResolvedValue([
      {
        id: 1, entityType: "SUPPLIER", enterpriseKey: 1,
        enterpriseCode: "B125", sourceSystem: "ERP", sourceCode: "ERP-1",
        name: "B125供应商",
      },
    ]);
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onDone?.({
        runId: "r-code", status: "SUCCEEDED", answer: "ok",
        pauseCode: null, errorCode: null, errorMessage: null,
        totalTokens: 1, cost: 0, stepsUrl: "/api/v1/agent-runs/x/steps",
      });
    });

    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));
    const supplierSelect = screen.getAllByRole("combobox")[1];
    fireEvent.mouseDown(supplierSelect);
    fireEvent.change(supplierSelect, { target: { value: "B125" } });
    await waitFor(() => expect(api.searchMappings).toHaveBeenCalled(), { timeout: 2000 });
    fireEvent.click(await waitFor(() => screen.getByText("B125供应商（B125）"), { timeout: 3000 }));

    fireEvent.change(screen.getByPlaceholderText(/补充说明/), {
      target: { value: "分析一下B109待到货情况" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(payloads).toHaveLength(1));
    // P4 新契约：B109 纯编码也让位的判定在后端（codePattern 配置），
    // 前端只保证文字与槽位完整上送
    const slots = payloads[0].slots as Record<string, unknown>;
    expect(slots.supplierKeys).toEqual(["B125"]);
    expect(String(payloads[0].message)).toContain("B109");
  });
});


/** 到货跟踪场景配置（与 backend/scripts/seed_copilot_scenarios.py 同构）。 */
const DELIVERY_TRACKING_CONFIG: CopilotScenario = {
  scenarioCode: "delivery_tracking",
  name: "到货跟踪",
  description: "按周期/供应商跟踪订单到货达成，含已到/未到物料明细",
  iconCode: "car",
  agentCode: "DELIVERY_TRACKING_AGENT",
  slotSchema: [
    { key: "monthsBack", type: "number", label: "时间窗" },
    { key: "anchorDay", type: "number", label: "月度周期锚点日" },
    { key: "periodIndex", type: "number", label: "周期期序" },
    { key: "supplierKeys", type: "entityMulti", label: "供应商" },
    { key: "orderKeys", type: "entityMulti", label: "订单" },
    { key: "dateFrom", type: "text", label: "开始日期", hidden: true },
    { key: "dateTo", type: "text", label: "结束日期", hidden: true },
  ],
  questionTemplate: "分析订单到货情况",
  chips: [
    { label: "近 12 个月到货概览", monthsBack: 12 },
    { label: "近 3 个月到货概览", monthsBack: 3 },
    { label: "本期到货概览（24 日周期）", anchorDay: 24, periodIndex: 0 },
    { label: "上期到货概览（24 日周期）", anchorDay: 24, periodIndex: -1 },
  ],
  resultRenderer: "delivery_tracking",
  requiredRole: null,
};

describe("ProcurementCopilotPage 到货跟踪渲染器分发", () => {
  it("result_renderer=delivery_tracking ⇒ 渲染到货跟踪卡（订单到货率为主指标）", async () => {
    mockOptions();
    api.listCopilotScenarios.mockResolvedValue([
      SCENARIO_CONFIG,
      DELIVERY_TRACKING_CONFIG,
    ]);
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      // 必须在 onDone **之前**就位：onDone 同步触发 refreshSteps（实测踩坑）
      api.getAgentRunSteps.mockResolvedValue([
        {
          stepNo: 1, iterationNo: 1, kind: "TOOL", status: "SUCCEEDED",
          action: null, planSummary: null, toolName: "delivery_tracking_query",
          inputJson: null,
          outputJson: { text: JSON.stringify({ data: {
            dataLayer: "thbi_dwd",
            window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
            summary: {
              totalLines: 19908, totalOrders: 4844,
              orderedQty: 98495265.255, receivedQty: 39904579.255,
              orderArrivalRate: 82.29, qtyArrivalRate: 40.51,
              allArrivedOrders: 3986, partialArrivedOrders: 448,
              notArrivedOrders: 410, excludedRows: 0,
            },
            bySupplier: [{
              supplierCode: "B125",
              supplierName: "浙江力航汽车部件有限公司",
              totalOrders: 29, allArrivedOrders: 1, partialArrivedOrders: 16,
              notArrivedOrders: 12, orderArrivalRate: 3.45,
              orderedQty: 15155100, receivedQty: 2176026,
            }],
            byOrder: [],
          }, answer: "" }), truncated: false },
          answer: null, errorCode: null, errorMessage: null,
          promptTokens: 0, completionTokens: 0, cost: 0,
          startedAt: "2026-11-24T00:00:00Z", finishedAt: "2026-11-24T00:00:01Z",
        },
      ]);
      handlers.onDone?.({
        runId: "r-dt", status: "SUCCEEDED", answer: "到货概览",
        pauseCode: null, errorCode: null, errorMessage: null,
        totalTokens: 1, cost: 0, stepsUrl: "/api/v1/agent-runs/r-dt/steps",
      });
    });

    render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <ConfigProvider locale={zhCN}>
            <ProcurementCopilotPage />
          </ConfigProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );
    fireEvent.click(await waitFor(() => screen.getByText("到货跟踪")));
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());

    const input = await waitFor(() => screen.getByPlaceholderText(/补充说明/));
    fireEvent.change(input, { target: { value: "看近 12 个月到货情况" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(payloads).toHaveLength(1));
    expect(payloads[0].scenarioCode).toBe("delivery_tracking");

    // 新卡的标志性内容（主指标 / 供应商表），且未误渲染待到货卡
    await waitFor(() =>
      expect(screen.getByTestId("delivery-primary-rate").textContent).toBe("82.29%"),
    );
    expect(screen.getAllByText("订单到货率").length).toBeGreaterThan(0);
    expect(screen.getByText("浙江力航汽车部件有限公司")).toBeTruthy();
    expect(screen.queryByText("待到货分析结果")).toBeNull();
  });

  it("进场景即显示「本期业务现状」概览卡（零 LLM：直接读只读统计端点）", async () => {
    mockOptions();
    api.listCopilotScenarios.mockResolvedValue([
      SCENARIO_CONFIG,
      DELIVERY_TRACKING_CONFIG,
    ]);

    render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <ConfigProvider locale={zhCN}>
            <ProcurementCopilotPage />
          </ConfigProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );
    fireEvent.click(await waitFor(() => screen.getByText("到货跟踪")));

    // 进场景就拉概览（不等用户提问）
    await waitFor(() => expect(api.getDeliveryTrackingOverview).toHaveBeenCalled());
    // 默认口径 = 本期（24 日结算周期）：只带供应商过滤，不带 monthsBack
    expect(api.getDeliveryTrackingOverview).toHaveBeenCalledWith({
      supplierKeys: [],
    });
    // 卡上显示本期窗口与主指标
    await waitFor(() => expect(screen.getByText(/2026-10-24 ~ 2026-11-24/)).toBeTruthy());
    expect(screen.getAllByText("订单到货率").length).toBeGreaterThan(0);
    // 未误渲染待到货欢迎态
    expect(screen.queryByText("业务现状（实时）")).toBeNull();
  });
});


describe("ProcurementCopilotPage 推荐问题 chips（对所有场景生效）", () => {
  it("到货跟踪场景显示配置里的 4 条推荐问题（非待到货专属）", async () => {
    mockOptions();
    api.listCopilotScenarios.mockResolvedValue([
      SCENARIO_CONFIG,
      DELIVERY_TRACKING_CONFIG,
    ]);
    render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <ConfigProvider locale={zhCN}>
            <ProcurementCopilotPage />
          </ConfigProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );
    fireEvent.click(await waitFor(() => screen.getByText("到货跟踪")));
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());

    // 2026-11-26 缺陷回归：chips 原先只在待到货欢迎态渲染 ⇒ 到货跟踪的
    // 4 条 chips 是死配置（配置在、UI 不显示）
    expect(screen.getByText("试试这样问我")).toBeTruthy();
    for (const chip of DELIVERY_TRACKING_CONFIG.chips) {
      expect(screen.getByText(chip.label)).toBeTruthy();
    }
  });

  it("点「近 12 个月到货概览」chip 按预设槽位发问", async () => {
    mockOptions();
    api.listCopilotScenarios.mockResolvedValue([
      SCENARIO_CONFIG,
      DELIVERY_TRACKING_CONFIG,
    ]);
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onDone?.({
        runId: "r-chip",
        status: "SUCCEEDED",
        answer: "ok",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 1,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-chip/steps",
      });
    });

    render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <ConfigProvider locale={zhCN}>
            <ProcurementCopilotPage />
          </ConfigProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );
    fireEvent.click(await waitFor(() => screen.getByText("到货跟踪")));
    fireEvent.click(await waitFor(() => screen.getByText("近 12 个月到货概览")));

    await waitFor(() => expect(payloads).toHaveLength(1));
    expect(payloads[0].scenarioCode).toBe("delivery_tracking");
    const slots = payloads[0].slots as Record<string, unknown>;
    expect(slots.monthsBack).toBe(12);
    expect("anchorDay" in slots).toBe(false);
  });

  it("待到货场景仍显示业务现状统计与推荐问题（回归守卫）", async () => {
    mockOptions();
    render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <ConfigProvider locale={zhCN}>
            <ProcurementCopilotPage />
          </ConfigProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );
    fireEvent.click(await waitFor(() => screen.getByText("待到货 / 逾期未到")));
    await waitFor(() =>
      expect(screen.getByText(/你好，我是采购待到货分析助手/)).toBeTruthy(),
    );
    // 业务现状统计仍是待到货专属
    expect(screen.getByText("业务现状（实时）")).toBeTruthy();
    // 推荐问题仍可见
    expect(screen.getByText("试试这样问我")).toBeTruthy();
    expect(screen.getByText("全部供应商概览（近 12 个月）")).toBeTruthy();
  });
});


describe("场景槽位默认值（anchorDay=0 不得劫持取数窗口）", () => {
  it("非 monthsBack 的数字槽位默认留空（不是 0）", () => {
    const values = defaultSlotValues(DELIVERY_TRACKING_CONFIG);
    expect(values.monthsBack).toBe(12);
    expect("anchorDay" in values).toBe(false);
    expect("periodIndex" in values).toBe(false);
    expect(values.anchorDay).toBeUndefined();
    expect(values.supplierKeys).toEqual([]);
  });

  it("点「近 12 个月」chip 上送的 slots 不含 anchorDay / periodIndex", () => {
    const slots = buildChipSlots(
      DELIVERY_TRACKING_CONFIG,
      DELIVERY_TRACKING_CONFIG.chips[0],
    );
    expect(slots.monthsBack).toBe(12);
    expect("anchorDay" in slots).toBe(false);
    expect("periodIndex" in slots).toBe(false);
    expect("dateFrom" in slots).toBe(false);
  });

  it("点「本期」chip 上送 anchorDay=24 / periodIndex=0", () => {
    const slots = buildChipSlots(
      DELIVERY_TRACKING_CONFIG,
      DELIVERY_TRACKING_CONFIG.chips[2],
    );
    expect(slots.anchorDay).toBe(24);
    expect(slots.periodIndex).toBe(0);
  });
});


describe("数据源下拉已彻底移除", () => {
  it("admin：高级参数里也不出现「数据源」标签与下拉", async () => {
    authState.roles = ["admin"];
    mockOptions();
    await renderScenario();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /高级参数/ }));

    expect(screen.queryByText("数据源")).toBeNull();
    // 但其他槽位仍在
    expect(screen.getByText("时间窗")).toBeTruthy();
  });
});
