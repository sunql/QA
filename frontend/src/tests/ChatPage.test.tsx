import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import { MemoryRouter } from "react-router-dom";
import zhCN from "antd/locale/zh_CN";
import type { DataSource } from "../types/datasource";

const { mockDataSource, chatApi, chatHistoryApi, exportCharts } = vi.hoisted(() => {
  const mockDataSource: DataSource = {
    id: 1,
    name: "ZJTH-Oracle",
    type: "oracle",
    host: "192.168.205.70",
    port: 1521,
    databaseName: "X3V71ORA",
    username: "ZJTH",
    description: null,
    isActive: true,
    isDefault: true,
    oracleVersion: "11g",
    createdBy: "system",
    createdTime: "2026-08-12T00:00:00Z",
    updatedTime: "2026-08-12T00:00:00Z",
  };
  const chatApi = {
    sendMessage: vi.fn(),
    sendMessageStream: vi.fn(),
    listDataSources: vi.fn(),
    getSuggestions: vi.fn(),
  };
  const chatHistoryApi = {
    exportSessionPdf: vi.fn(),
    // 挂载期回放会用到这两个（ChatPage → chatStore.enterChannel）：
    // mock 少一个函数不会报错，只会「调用返回 undefined」被 store 的 try/catch 吞掉，
    // 表现为页面多出一条红色 Alert —— 这类残缺替身正是缺陷能隐身的老套路。
    listChatSessions: vi.fn(),
    loadSessionMessages: vi.fn(),
  };
  // 导出前的截图收集（0105）：单测只关心「有没有被调、结果有没有进请求体」，
  // 真正的离屏渲染在 collectExportCharts / chartSnapshot 自己的测试里。
  const exportCharts = {
    collectExportCharts: vi.fn(),
  };
  return { mockDataSource, chatApi, chatHistoryApi, exportCharts };
});

vi.mock("../api/datasource", () => ({
  listDataSources: (...args: unknown[]) => chatApi.listDataSources(...args),
}));
vi.mock("../api/chat", () => ({
  sendMessage: (...args: unknown[]) => chatApi.sendMessage(...args),
  sendMessageStream: (...args: unknown[]) => chatApi.sendMessageStream(...args),
  getSuggestions: (...args: unknown[]) => chatApi.getSuggestions(...args),
}));
vi.mock("../api/chatHistory", () => ({
  exportSessionPdf: (...args: unknown[]) => chatHistoryApi.exportSessionPdf(...args),
  listChatSessions: (...args: unknown[]) => chatHistoryApi.listChatSessions(...args),
  loadSessionMessages: (...args: unknown[]) => chatHistoryApi.loadSessionMessages(...args),
}));
vi.mock("../utils/collectExportCharts", () => ({
  collectExportCharts: (...args: unknown[]) => exportCharts.collectExportCharts(...args),
}));
vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: () => <div data-testid="echarts-mock">chart</div>,
}));

import ChatPage from "../pages/ChatPage";
import { useChatStore } from "../stores/chatStore";

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      {/* ChatPanel 用 useLocation（历史面板按路由持久化），测试需 Router 上下文 */}
      <MemoryRouter>
        <ChatPage />
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("ChatPage", () => {
  beforeEach(() => {
    // 清掉真实 localStorage：store 的持久化层在本文件里**没有**被 mock，
    // 于是「上一个用例发送消息时写下的会话恢复指针」会留到这里 —— 挂载期的
    // 刷新恢复会真的触发，把 sessionId 换成指针里的那个（导出断言随即失败）。
    // 用例间的持久化状态必须显式清掉，别指望它恰好是空的。
    window.localStorage.clear();
    vi.clearAllMocks();
    useChatStore.setState({
      messages: [],
      sessionId: "s-test",
      loading: false,
      datasourceId: null,
      error: null,
      channel: "chat",
      sessions: [],
      sessionsError: null,
      historyPanelOpen: false,
    });
    chatApi.listDataSources.mockResolvedValue([mockDataSource]);
    chatApi.getSuggestions.mockResolvedValue([]);
    // 替身必须有真实形状：默认返回 undefined 会让 store 把 sessions 设成 undefined，
    // 渲染期直接炸（比「函数不存在」更糟 —— 后者至少会被 store 的 try/catch 兜住）。
    chatHistoryApi.listChatSessions.mockResolvedValue([]);
    chatHistoryApi.loadSessionMessages.mockResolvedValue({ sessionId: "s-test", messages: [] });
  });

  it("渲染标题与空状态", () => {
    renderPage();
    expect(screen.getByText("AIChatService")).toBeInTheDocument();
    expect(screen.getByText("输入问题开始对话")).toBeInTheDocument();
  });

  it("挂载即声明 chat 渠道并按恢复指针回放上次会话（刷新恢复的接线）", async () => {
    // 模拟「上次会话留下的指针」——store 的持久化层在本文件里没被 mock，读的是真 localStorage
    window.localStorage.setItem("qa:chat:lastSessionId:chat", "chat-last");
    chatHistoryApi.loadSessionMessages.mockResolvedValue({
      sessionId: "chat-last",
      messages: [
        {
          id: 7,
          role: "assistant",
          content: "上一轮的回答",
          question: null,
          sql: null,
          createdTime: "2026-09-30T10:00:00Z",
          interrupted: false,
        },
      ],
    });

    renderPage();

    await waitFor(() =>
      expect(chatHistoryApi.loadSessionMessages).toHaveBeenCalledWith("chat-last", undefined, {
        tail: true,
      })
    );
    expect(await screen.findByText("上一轮的回答")).toBeInTheDocument();
    expect(useChatStore.getState().sessionId).toBe("chat-last");
  });

  it("发送问题后通过 SSE 流式展示用户与助手消息、SQL 与图表", async () => {
    const user = userEvent.setup();
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onSql?.("SELECT NAME FROM ZJTH.PRECEIPT");
      handlers.onChart?.({
        chartType: "bar",
        chartOption: { series: [{ type: "bar" }] },
        data: [{ NAME: "A" }],
      });
      handlers.onToken?.("查询完成，");
      handlers.onToken?.("共 2 条记录。");
      handlers.onDone?.({ tokensUsed: 45, cost: 0.00006 });
    });
    renderPage();

    // 等待默认数据源选中
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });

    await user.type(
      screen.getByPlaceholderText(/输入自然语言问题/),
      "各供应商的收货数量汇总"
    );
    await user.click(screen.getByRole("button", { name: /发\s?送/ }));

    // 用户消息 + 助手消息
    expect(screen.getByText("各供应商的收货数量汇总")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("查询完成，共 2 条记录。")).toBeInTheDocument();
    });

    // 请求走流式端点，负载校验
    const payload = chatApi.sendMessageStream.mock.calls[0][0];
    expect(payload.datasourceId).toBe(1);
    expect(payload.question).toBe("各供应商的收货数量汇总");
    expect(payload.sessionId).toBe("s-test");
    expect(chatApi.sendMessage).not.toHaveBeenCalled();

    // SQL 可展开查看
    expect(screen.getByText("查看 SQL")).toBeInTheDocument();
    expect(screen.getByTestId("echarts-mock")).toBeInTheDocument();
    // Token 与成本标签
    expect(screen.getByText("Tokens: 45")).toBeInTheDocument();
    expect(screen.getByText("成本: $0.000060")).toBeInTheDocument();
  });

  it("选择图表类型后随请求携带 chartType", async () => {
    const user = userEvent.setup();
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onSql?.("SELECT 1");
      handlers.onChart?.({ chartType: "line", chartOption: {}, data: [] });
      handlers.onDone?.({ tokensUsed: 10, cost: 0 });
    });
    renderPage();
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });

    await user.type(
      screen.getByPlaceholderText(/输入自然语言问题/),
      "各供应商的收货数量汇总"
    );
    await user.click(screen.getByRole("combobox", { name: /图表类型/ }));
    await user.click(screen.getByText("折线图"));
    await user.click(screen.getByRole("button", { name: /发\s?送/ }));

    await waitFor(() => {
      const payload = chatApi.sendMessageStream.mock.calls[0][0];
      expect(payload.chartType).toBe("line");
    });
  });

  it("领域命令意图（define）响应正常展示并入库", async () => {
    const user = userEvent.setup();
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onMeta?.("define");
      handlers.onToken?.("指标「销售额」已定义：SUM(order.amount)");
      handlers.onDone?.({ tokensUsed: 0, cost: 0, modelName: null });
    });
    renderPage();
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });

    await user.type(
      screen.getByPlaceholderText(/输入自然语言问题/),
      "定义指标 销售额 = SUM(order.amount)"
    );
    await user.click(screen.getByRole("button", { name: /发\s?送/ }));

    await waitFor(() => {
      expect(screen.getByText(/指标「销售额」已定义/)).toBeInTheDocument();
    });
    // meta 意图（define）已被 KNOWN_INTENTS 收窄入库
    const msgs = useChatStore.getState().messages;
    expect(msgs[msgs.length - 1].intent).toBe("define");
  });

  it("斜杠指令 /metric 直接发送由后端解析为 metric 意图", async () => {
    const user = userEvent.setup();
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onMeta?.("metric");
      handlers.onToken?.("指标「销售额」已定义：SUM(order.amount)");
      handlers.onDone?.({ tokensUsed: 0, cost: 0, modelName: null });
    });
    renderPage();
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });

    await user.type(
      screen.getByPlaceholderText(/输入自然语言问题/),
      "/metric 销售额 = SUM(order.amount)"
    );
    await user.click(screen.getByRole("button", { name: /发\s?送/ }));

    // 校验请求体：原始斜杠文本原样发送，由后端识别
    const payload = chatApi.sendMessageStream.mock.calls[0][0];
    expect(payload.question).toBe("/metric 销售额 = SUM(order.amount)");

    await waitFor(() => {
      expect(screen.getByText(/指标「销售额」已定义/)).toBeInTheDocument();
    });
    const msgs = useChatStore.getState().messages;
    expect(msgs[msgs.length - 1].intent).toBe("metric");
    // 零消耗：领域命令不计入 tokenUsage
    expect(screen.getByText("Tokens: 0")).toBeInTheDocument();
  });

  it("数据源加载失败时界面仍可正常渲染", async () => {
    chatApi.listDataSources.mockRejectedValue(new Error("网络异常"));
    renderPage();
    // 不崩溃且仍显示空状态
    await waitFor(() => {
      expect(screen.getByText("输入问题开始对话")).toBeInTheDocument();
    });
  });

  it("接口失败时展示错误消息", async () => {
    const user = userEvent.setup();
    chatApi.sendMessageStream.mockRejectedValue(new Error("服务不可用"));
    renderPage();
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });

    await user.type(screen.getByPlaceholderText(/输入自然语言问题/), "查询");
    await user.click(screen.getByRole("button", { name: /发\s?送/ }));

    await waitFor(() => {
      expect(screen.getByText("服务不可用")).toBeInTheDocument();
    });
  });

  it("SSE done 携带 affinityStatus 时渲染锁定 badge", async () => {
    const user = userEvent.setup();
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onSql?.("SELECT 1");
      handlers.onChart?.({ chartType: "bar", chartOption: {}, data: [{ X: 1 }] });
      handlers.onToken?.("查询完成");
      handlers.onDone?.({
        tokensUsed: 10,
        cost: 0.001,
        modelName: "deepseek-chat",
        affinityStatus: { lockedModel: "deepseek-chat", remainingTurns: 2 },
      });
    });
    renderPage();
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });

    await user.type(screen.getByPlaceholderText(/输入自然语言问题/), "查询");
    await user.click(screen.getByRole("button", { name: /发\s?送/ }));

    await waitFor(() => {
      expect(screen.getByText("查询完成")).toBeInTheDocument();
    });
    // Phase 7：锁定 badge 显示「🔒 锁定 deepseek-chat · 剩 2 轮」
    expect(screen.getByText(/锁定 deepseek-chat · 剩 2 轮/)).toBeInTheDocument();
    // store 已保存 affinityStatus
    const msgs = useChatStore.getState().messages;
    const last = msgs[msgs.length - 1];
    expect(last?.affinityStatus?.lockedModel).toBe("deepseek-chat");
    expect(last?.affinityStatus?.remainingTurns).toBe(2);
  });

  it("SSE done 未携带 affinityStatus 时不渲染锁定 badge", async () => {
    const user = userEvent.setup();
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onSql?.("SELECT 1");
      handlers.onChart?.({ chartType: "bar", chartOption: {}, data: [{ X: 1 }] });
      handlers.onToken?.("查询完成");
      handlers.onDone?.({ tokensUsed: 10, cost: 0.001, modelName: "deepseek-chat" });
    });
    renderPage();
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });

    await user.type(screen.getByPlaceholderText(/输入自然语言问题/), "查询");
    await user.click(screen.getByRole("button", { name: /发\s?送/ }));

    await waitFor(() => {
      expect(screen.getByText("查询完成")).toBeInTheDocument();
    });
    expect(screen.queryByText(/锁定.*剩.*轮/)).not.toBeInTheDocument();
  });

  // =========================================================================
  // 历史会话面板布局切换（feat-chat-history-panel）
  // =========================================================================

  it("默认 historyPanelOpen=false 时只渲染浮起按钮，不渲染面板", () => {
    // 进入测试时 historyPanelOpen 默认为 false（resetStore 未显式设置，但 zustand 默认初始化为 false）
    useChatStore.setState({ historyPanelOpen: false });
    renderPage();
    // 浮起按钮：aria-label="展开历史"
    expect(screen.getByRole("button", { name: "展开历史" })).toBeInTheDocument();
    // 面板标题「历史问答」不出现
    expect(screen.queryByText("历史问答")).not.toBeInTheDocument();
  });

  it("historyPanelOpen=true 时渲染面板且不渲染浮起按钮", () => {
    useChatStore.setState({ historyPanelOpen: true });
    renderPage();
    expect(screen.getByText("历史问答")).toBeInTheDocument();
    // 浮起按钮 aria-label 不出现
    expect(screen.queryByRole("button", { name: "展开历史" })).not.toBeInTheDocument();
  });

  it("toggleHistoryPanel 切换渲染：false→true 显示面板，true→false 显示按钮", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ historyPanelOpen: false });
    renderPage();
    // 折叠态：浮起按钮在；面板标题不在
    expect(screen.getByRole("button", { name: "展开历史" })).toBeInTheDocument();
    expect(screen.queryByText("历史问答")).not.toBeInTheDocument();

    // 点击浮起按钮展开
    await user.click(screen.getByRole("button", { name: "展开历史" }));
    await waitFor(() => {
      expect(screen.getByText("历史问答")).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: "展开历史" })).not.toBeInTheDocument();
    // 状态已翻转为 true
    expect(useChatStore.getState().historyPanelOpen).toBe(true);
  });
});

describe("ChatPage PDF 导出", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({
      messages: [],
      sessionId: "s-test",
      loading: false,
      datasourceId: null,
      error: null,
    });
    chatApi.listDataSources.mockResolvedValue([mockDataSource]);
    chatApi.getSuggestions.mockResolvedValue([]);
    // jsdom Blob 无 .text()，但可被 axios responseType="blob" 透传；
    // 这里构造一个最小可下载 Blob 即可
    chatHistoryApi.exportSessionPdf.mockResolvedValue(
      new Blob(["%PDF-1.4 mock"], { type: "application/pdf" })
    );
    exportCharts.collectExportCharts.mockResolvedValue([]);
  });

  it("messages 为空时点击全局导出按钮弹出 warning，不调 API", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });
    await user.click(screen.getByRole("button", { name: "导出当前会话为 PDF" }));
    expect(chatHistoryApi.exportSessionPdf).not.toHaveBeenCalled();
    // 没消息就不该白跑一趟截图
    expect(exportCharts.collectExportCharts).not.toHaveBeenCalled();
  });

  it("有 messages 时点击全局导出按钮调用 exportSessionPdf(currentSessionId)", async () => {
    const user = userEvent.setup();
    // seed 一条 user + 一条 assistant 流式结束消息
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onToken?.("查询结果：12 条。");
      handlers.onDone?.({ tokensUsed: 20, cost: 0.0001 });
    });
    renderPage();
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });
    await user.type(
      screen.getByPlaceholderText(/输入自然语言问题/),
      "今天的销售"
    );
    await user.click(screen.getByRole("button", { name: /发\s?送/ }));
    await waitFor(() => {
      expect(screen.getByText("查询结果：12 条。")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "导出当前会话为 PDF" }));
    await waitFor(() => {
      expect(chatHistoryApi.exportSessionPdf).toHaveBeenCalledTimes(1);
    });
    // 第一个参数是 sessionId；第二个是导出请求体（0105 起 messageId 走 body）
    const call = chatHistoryApi.exportSessionPdf.mock.calls[0];
    expect(call[0]).toBe("s-test");
    expect(call[1]).toEqual({ messageId: undefined, charts: [] });
  });

  it("导出前先收集图表截图，并把位图放进请求体（0105 图表进最终报告）", async () => {
    const user = userEvent.setup();
    const charts = [{ messageId: 7, imagePng: "data:image/png;base64,AA==" }];
    exportCharts.collectExportCharts.mockResolvedValue(charts);
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onToken?.("查询结果：12 条。");
      handlers.onDone?.({ tokensUsed: 20, cost: 0.0001 });
    });
    renderPage();
    await waitFor(() => {
      expect(useChatStore.getState().datasourceId).toBe(1);
    });
    await user.type(screen.getByPlaceholderText(/输入自然语言问题/), "今天的销售");
    await user.click(screen.getByRole("button", { name: /发\s?送/ }));
    await waitFor(() => {
      expect(screen.getByText("查询结果：12 条。")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "导出当前会话为 PDF" }));

    await waitFor(() => {
      expect(chatHistoryApi.exportSessionPdf).toHaveBeenCalledWith("s-test", {
        messageId: undefined,
        charts,
      });
    });
    expect(exportCharts.collectExportCharts).toHaveBeenCalledWith("s-test", undefined);
  });

  it("单条导出把 dbMessageId 同时用于截图定位与导出范围", async () => {
    const user = userEvent.setup();
    exportCharts.collectExportCharts.mockResolvedValue([]);
    useChatStore.setState({
      messages: [
        {
          id: "m-test-export",
          role: "assistant",
          content: "已回填 dbId 的助手回答",
          timestamp: Date.now(),
          dbMessageId: 42,
        } as unknown as ReturnType<typeof useChatStore.getState>["messages"][number],
      ],
    });
    renderPage();

    await user.click(screen.getByRole("button", { name: /导出该条问答为 PDF/ }));

    await waitFor(() => {
      expect(chatHistoryApi.exportSessionPdf).toHaveBeenCalledWith("s-test", {
        messageId: 42,
        charts: [],
      });
    });
    expect(exportCharts.collectExportCharts).toHaveBeenCalledWith("s-test", 42);
  });

  it("MessageItem 单条导出按钮在 dbMessageId 已回填时显示", () => {
    // 在 store 中注入一条带 dbMessageId 的 assistant 消息
    useChatStore.setState({
      messages: [
        {
          id: "m-test-1",
          role: "assistant",
          content: "已回填 dbId 的助手回答",
          timestamp: Date.now(),
          sql: "SELECT 1",
          dbMessageId: 42,
        } as unknown as ReturnType<typeof useChatStore.getState>["messages"][number],
      ],
    });
    renderPage();
    // 单条按钮按 aria-label 查找（antd button 包含图标 + 文本）
    expect(
      screen.getByRole("button", { name: /导出该条问答为 PDF/ })
    ).toBeInTheDocument();
  });

  it("MessageItem 单条导出按钮在 dbMessageId 缺省时不显示", () => {
    useChatStore.setState({
      messages: [
        {
          id: "m-test-2",
          role: "assistant",
          content: "未回填 dbId 的助手回答",
          timestamp: Date.now(),
        } as unknown as ReturnType<typeof useChatStore.getState>["messages"][number],
      ],
    });
    renderPage();
    expect(
      screen.queryByRole("button", { name: /导出该条问答为 PDF/ })
    ).not.toBeInTheDocument();
  });

  it("点击单条导出按钮 → 调用 exportSessionPdf(sessionId, { messageId: dbMessageId })", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      messages: [
        {
          id: "m-test-3",
          role: "assistant",
          content: "可单条导出的消息",
          timestamp: Date.now(),
          dbMessageId: 99,
        } as unknown as ReturnType<typeof useChatStore.getState>["messages"][number],
      ],
    });
    renderPage();
    await user.click(screen.getByRole("button", { name: /导出该条问答为 PDF/ }));
    await waitFor(() =>
      expect(chatHistoryApi.exportSessionPdf).toHaveBeenCalledWith("s-test", {
        messageId: 99,
        charts: [],
      }),
    );
  });
});
