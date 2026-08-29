import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import UsagePage from "../pages/UsagePage";
import type {
  DailyUsageTrend,
  GlobalUsageSummary,
  ModelUsageAggregate,
  SessionListItem,
  SessionTokenUsage,
  TokenUsageSummary,
} from "../types/session";

// mock echarts-for-react：jsdom 无法实例化 ECharts 画布，渲染占位 div 即可
vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: () => <div data-testid="echarts-mock">chart</div>,
}));

const SESSIONS: SessionListItem[] = [
  {
    sessionId: "s-1",
    totalRequests: 2,
    totalTokens: 180,
    totalCost: 0.007,
    firstRequestTime: "2026-08-12T10:00:00Z",
    lastRequestTime: "2026-08-12T10:05:00Z",
    lastQuestion: "各供应商收货量",
  },
  {
    sessionId: "s-2",
    totalRequests: 1,
    totalTokens: 15,
    totalCost: 0.001,
    firstRequestTime: "2026-08-12T11:00:00Z",
    lastRequestTime: "2026-08-12T11:00:00Z",
    lastQuestion: null,
  },
];

const SUMMARY: TokenUsageSummary = {
  sessionId: "s-1",
  totalRequests: 2,
  totalTokens: 180,
  totalCost: 0.007,
  byModel: [
    { modelName: "deepseek-chat", requests: 1, totalTokens: 150, totalCost: 0.005 },
    { modelName: "cheap-model", requests: 1, totalTokens: 30, totalCost: 0.002 },
  ],
};

const ROWS: SessionTokenUsage[] = [
  {
    id: 1,
    sessionId: "s-1",
    modelName: "deepseek-chat",
    promptTokens: 100,
    completionTokens: 50,
    totalTokens: 150,
    cost: 0.005,
    requestTime: "2026-08-12T10:00:00Z",
    purpose: "nl2sql",
  },
  {
    id: 2,
    sessionId: "s-1",
    modelName: "cheap-model",
    promptTokens: 20,
    completionTokens: 10,
    totalTokens: 30,
    cost: 0.002,
    requestTime: "2026-08-12T10:05:00Z",
    purpose: "answer",
  },
];

// 数值选与会话表/明细卡均不冲突的取值，保证 getByText 唯一
const GLOBAL: GlobalUsageSummary = {
  totalSessions: 5,
  totalRequests: 9,
  totalTokens: 187,
  totalCost: 0.009,
};

const DAILY: DailyUsageTrend[] = [
  { date: "2026-08-10", requests: 1, tokens: 50, cost: 0.003 },
  { date: "2026-08-11", requests: 2, tokens: 137, cost: 0.006 },
];

const MODEL_USAGE: ModelUsageAggregate[] = [
  { modelName: "deepseek-chat", requests: 2, tokens: 180, cost: 0.007 },
  { modelName: "cheap-model", requests: 1, tokens: 7, cost: 0.002 },
];

const api = vi.hoisted(() => ({
  listSessions: vi.fn(),
  getSessionUsageSummary: vi.fn(),
  listSessionUsage: vi.fn(),
  getGlobalUsageSummary: vi.fn(),
  getDailyUsageTrends: vi.fn(),
  getModelUsage: vi.fn(),
}));

vi.mock("../api/session", () => api);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/usage"]}>
        <Routes>
          <Route path="/usage" element={<UsagePage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("UsagePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listSessions.mockResolvedValue(SESSIONS);
    api.getSessionUsageSummary.mockResolvedValue(SUMMARY);
    api.listSessionUsage.mockResolvedValue(ROWS);
    api.getGlobalUsageSummary.mockResolvedValue(GLOBAL);
    api.getDailyUsageTrends.mockResolvedValue(DAILY);
    api.getModelUsage.mockResolvedValue(MODEL_USAGE);
  });

  it("加载并渲染全局用量汇总卡片与两张趋势图表", async () => {
    renderPage();
    // 四张汇总卡片标题（与明细卡「调用次数/累计 Token」区分，避免重复匹配）
    expect(await screen.findByText("会话数")).toBeInTheDocument();
    expect(screen.getByText("请求数")).toBeInTheDocument();
    expect(screen.getByText("总 Token")).toBeInTheDocument();
    expect(screen.getByText("总成本($)")).toBeInTheDocument();
    // 汇总值（唯一取值）
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText("9")).toBeInTheDocument();
    expect(screen.getByText("187")).toBeInTheDocument();
    // Statistic 将整数与小数拆分为两个 span，匹配小数部分
    expect(screen.getByText(".009000")).toBeInTheDocument();
    // 两张图表（Token 趋势折线图 + 模型用量饼图）
    expect(screen.getAllByTestId("echarts-mock")).toHaveLength(2);
    // 三个全局聚合接口各调用一次
    await waitFor(() => {
      expect(api.getGlobalUsageSummary).toHaveBeenCalledTimes(1);
      expect(api.getDailyUsageTrends).toHaveBeenCalledTimes(1);
      expect(api.getModelUsage).toHaveBeenCalledTimes(1);
    });
    // 等会话明细加载完成，避免悬空 promise 干扰后续用例
    await screen.findByText("按模型汇总", undefined, { timeout: 3000 });
  });

  it("全局用量接口失败时看板仍可渲染会话列表", async () => {
    api.getGlobalUsageSummary.mockRejectedValue(new Error("网络异常"));
    api.getDailyUsageTrends.mockRejectedValue(new Error("网络异常"));
    api.getModelUsage.mockRejectedValue(new Error("网络异常"));
    renderPage();
    // 会话列表不受影响
    expect(await screen.findByText("各供应商收货量")).toBeInTheDocument();
    await screen.findByText("按模型汇总", undefined, { timeout: 3000 });
  });

  it("最近提问超出列宽时悬停 Tooltip 展示完整内容", async () => {
    const LONG_Q =
      "查询最近三十天内所有供应商的采购订单收货量与金额汇总，并按供应商分组排序。这是一条非常长的提问用于验证悬停查看完整内容。";
    api.listSessions.mockResolvedValue([{ ...SESSIONS[0], lastQuestion: LONG_Q }]);
    renderPage();
    await screen.findByText(LONG_Q);

    fireEvent.mouseEnter(screen.getByText(LONG_Q));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent(LONG_Q);

    // 等待自动选中的首个会话明细加载完成，避免悬空 promise 干扰后续测试
    await screen.findByText("按模型汇总", undefined, { timeout: 3000 });
  });

  it("加载并展示会话列表，含最近提问与成本", async () => {
    renderPage();
    await screen.findByText("各供应商收货量");
    expect(screen.getByText("s-1")).toBeInTheDocument();
    // 成本格式化（0.007000）
    expect(screen.getByText("0.007000")).toBeInTheDocument();
    // 等待自动选中的首个会话明细加载完成，避免悬空 promise 干扰后续测试
    await screen.findByText("按模型汇总", undefined, { timeout: 3000 });
  });

  it("默认选中首个会话并展示明细：调用次数/累计 token/成本与流水表", async () => {
    renderPage();
    // 明细区标题与按模型汇总卡片（findByText 等待异步明细加载）
    const title = await screen.findByText("按模型汇总", undefined, { timeout: 3000 });
    expect(title).toBeInTheDocument();
    // 流水表中的用途标签（唯一）
    expect(screen.getByText("nl2sql")).toBeInTheDocument();
    expect(screen.getByText("answer")).toBeInTheDocument();
  });

  it("点击刷新按钮重新拉取会话列表", async () => {
    renderPage();
    await screen.findByText("各供应商收货量");
    expect(api.listSessions).toHaveBeenCalledTimes(1);
    const refreshBtn = screen.getByRole("button", { name: /刷新/ });
    refreshBtn.click();
    await waitFor(() => expect(api.listSessions).toHaveBeenCalledTimes(2));
  });

  it("会话列表为空时不渲染明细区", async () => {
    api.listSessions.mockResolvedValue([]);
    renderPage();
    await waitFor(() => expect(api.listSessions).toHaveBeenCalledTimes(1));
    // 无明细卡片
    expect(screen.queryByText(/会话用量明细/)).not.toBeInTheDocument();
  });
});
