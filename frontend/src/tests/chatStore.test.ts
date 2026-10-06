import { describe, it, expect, vi, beforeEach } from "vitest";

const chatApi = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  sendMessageStream: vi.fn(),
  resumeMultiStepRun: vi.fn(),
  fetchHypotheses: vi.fn(),
}));
vi.mock("../api/chat", () => chatApi);

const historyApi = vi.hoisted(() => ({
  listChatSessions: vi.fn(),
  loadSessionMessages: vi.fn(),
  deleteSessionHistory: vi.fn(),
}));
vi.mock("../api/chatHistory", () => historyApi);

const persist = vi.hoisted(() => ({
  readLastSessionId: vi.fn<(channel: string) => string | null>(() => null),
  writeLastSessionId: vi.fn<(channel: string, sessionId: string | null) => void>(),
  readLastChannel: vi.fn<() => string | null>(() => null),
  writeLastChannel: vi.fn<(channel: string) => void>(),
  readHistoryPanelOpen: vi.fn<() => boolean>(() => false),
  writeHistoryPanelOpen: vi.fn<(open: boolean) => void>(),
}));
vi.mock("../stores/persistChatUiState", () => persist);

import { useChatStore, generateSessionId } from "../stores/chatStore";
import type { StreamEventHandlers } from "../api/chat";
import type { ChatMessage } from "../types/chat";
import type { ChatSession, ChatMessageRead } from "../types/chatHistory";

function resetStore() {
  useChatStore.setState({
    messages: [],
    sessionId: "s-test",
    loading: false,
    datasourceId: null,
    error: null,
    sessions: [],
    sessionsLoading: false,
    sessionsError: null,
    historyPanelOpen: false,
  });
}

describe("chatStore", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it("generateSessionId 生成非空会话 id", () => {
    const id = generateSessionId();
    expect(id.startsWith("s-")).toBe(true);
    expect(id.length).toBeGreaterThan(3);
  });

  it("addMessage 追加消息（不可变）", () => {
    const m1: ChatMessage = { id: "1", role: "user", content: "你好", timestamp: 1 };
    const m2: ChatMessage = { id: "2", role: "assistant", content: "你好！", timestamp: 2 };
    useChatStore.getState().addMessage(m1);
    useChatStore.getState().addMessage(m2);
    const messages = useChatStore.getState().messages;
    expect(messages).toHaveLength(2);
    expect(messages[0]).toBe(m1);
    expect(messages[1]).toBe(m2);
  });

  it("clearMessages 清空消息", () => {
    useChatStore.getState().addMessage({ id: "1", role: "user", content: "x", timestamp: 1 });
    useChatStore.getState().clearMessages();
    expect(useChatStore.getState().messages).toHaveLength(0);
  });

  it("sendMessage 成功时写入用户与助手消息", async () => {
    chatApi.sendMessage.mockResolvedValue({
      answer: "查询完成",
      intent: "query",
      sql: "SELECT 1",
      chartType: "pie",
      chartOption: { series: [] },
      data: [],
      tokensUsed: 45,
      cost: 0.00006,
      modelName: "deepseek-chat",
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("各供应商的收货数量汇总");

    const state = useChatStore.getState();
    expect(state.loading).toBe(false);
    expect(state.messages).toHaveLength(2);
    expect(state.messages[0].role).toBe("user");
    expect(state.messages[1].role).toBe("assistant");
    expect(state.messages[1].content).toBe("查询完成");
    expect(state.messages[1].sql).toBe("SELECT 1");
    expect(state.messages[1].modelName).toBe("deepseek-chat");
    // 请求负载含会话与历史
    expect(chatApi.sendMessage).toHaveBeenCalledTimes(1);
    const payload = chatApi.sendMessage.mock.calls[0][0];
    expect(payload.sessionId).toBe("s-test");
    expect(payload.datasourceId).toBe(1);
    expect(payload.history).toEqual([]);
  });

  it("sendMessage 非流式响应回填 ReAct 查询计划与多轮意图（Phase C/E）", async () => {
    chatApi.sendMessage.mockResolvedValue({
      answer: "查询完成",
      intent: "refine",
      sql: "SELECT 1",
      chartType: "pie",
      chartOption: { series: [] },
      data: [],
      queryPlan: {
        target: "各供应商的收货数量汇总",
        selectedClasses: ["PRECEIPT"],
        selectedProperties: ["NAME", "QTY"],
        aggregations: [],
        groupBy: ["NAME"],
        conditions: [],
        joins: [],
        sortBy: [{ property: "NAME", direction: "asc" }],
        rowLimit: 10,
      },
      tokensUsed: 45,
      cost: 0.00006,
      modelName: "deepseek-chat",
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("按数量升序排列");

    const assistant = useChatStore.getState().messages[1];
    expect(assistant.queryPlan?.target).toBe("各供应商的收货数量汇总");
    expect(assistant.queryPlan?.selectedClasses).toEqual(["PRECEIPT"]);
    expect(assistant.queryPlan?.rowLimit).toBe(10);
    expect(assistant.intent).toBe("refine");
  });

  it("sendMessage 失败时写入错误助手消息", async () => {
    chatApi.sendMessage.mockRejectedValue(new Error("服务不可用"));
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("查询");

    const state = useChatStore.getState();
    expect(state.loading).toBe(false);
    expect(state.error).toBe("服务不可用");
    expect(state.messages[1].isError).toBe(true);
  });

  it("非流式多步：失败步骤（error 非空）标记为 error 而非 done（C3 失败隔离）", async () => {
    chatApi.sendMessage.mockResolvedValue({
      answer: "多步执行失败：0/2 步完成",
      intent: "multi_step",
      tokensUsed: 10,
      cost: 0.00001,
      steps: [
        {
          stepIndex: 0,
          description: "2024 销售额",
          subQuestion: "2024年的销售额是多少",
          sql: "SELECT 1",
          summary: "1000",
          error: null,
        },
        {
          stepIndex: 1,
          description: "2025 销售额",
          subQuestion: "2025年的销售额是多少",
          sql: null,
          summary: null,
          error: "该步骤执行失败：ORA-00942: 表或视图不存在",
        },
      ],
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("分步查询 2024 和 2025 的销售额并对比");

    const steps = useChatStore.getState().messages[1].steps;
    expect(steps?.[0]).toMatchObject({ status: "done", sql: "SELECT 1" });
    expect(steps?.[1]).toMatchObject({
      status: "error",
      error: "该步骤执行失败：ORA-00942: 表或视图不存在",
    });
  });

  it("非流式多步：每步自己的 chartType/chartOption 回填到 step（多步每步出图）", async () => {
    chatApi.sendMessage.mockResolvedValue({
      answer: "两步都完成了",
      intent: "multi_step",
      tokensUsed: 30,
      cost: 0.00003,
      steps: [
        {
          stepIndex: 0,
          description: "各供应商收货量",
          subQuestion: "各供应商的收货量",
          sql: "SELECT 1",
          summary: null,
          error: null,
          chartType: "hbar",
          chartOption: { series: [{ type: "bar", data: [1] }] },
        },
        {
          stepIndex: 1,
          description: "失败的步骤",
          subQuestion: "查不到的表",
          sql: null,
          summary: null,
          error: "ORA-00942",
        },
      ],
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("分步查询");

    const steps = useChatStore.getState().messages[1].steps;
    expect(steps?.[0]).toMatchObject({ chartType: "hbar" });
    expect(steps?.[0]?.chartOption).toEqual({ series: [{ type: "bar", data: [1] }] });
    // 失败步骤没有图：两字段为 null，渲染层据此不画
    expect(steps?.[1]?.chartType ?? null).toBeNull();
    expect(steps?.[1]?.chartOption ?? null).toBeNull();
  });

  // =========================================================================
  // 流式输出（5.6）
  // =========================================================================

  it("sendMessage 流式模式下逐 token 累积并落到占位消息", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onSql?.("SELECT 1 FROM DUAL");
      handlers.onChart?.({
        chartType: "pie",
        chartOption: { series: [] },
        data: [{ NAME: "A" }],
      });
      handlers.onToken?.("查询完成，");
      handlers.onToken?.("共 2 条记录。");
      handlers.onDone?.({ tokensUsed: 45, cost: 0.00006, modelName: "deepseek-chat" });
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("各供应商的收货数量汇总", true);

    const state = useChatStore.getState();
    expect(state.loading).toBe(false);
    expect(state.messages).toHaveLength(2);
    expect(state.messages[1].content).toBe("查询完成，共 2 条记录。");
    expect(state.messages[1].sql).toBe("SELECT 1 FROM DUAL");
    expect(state.messages[1].chartType).toBe("pie");
    expect(state.messages[1].tokensUsed).toBe(45);
    expect(state.messages[1].modelName).toBe("deepseek-chat");
    expect(state.messages[1].isStreaming).toBe(false);
    // 走流式端点时不再调用非流式接口
    expect(chatApi.sendMessage).not.toHaveBeenCalled();
  });

  it("sendMessage 流式 error 事件写入错误消息", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onError?.("数据源 99999 不存在");
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("查询", true);

    const state = useChatStore.getState();
    expect(state.messages[1].isError).toBe(true);
    expect(state.messages[1].content).toBe("数据源 99999 不存在");
    expect(state.messages[1].isStreaming).toBe(false);
    expect(state.loading).toBe(false);
    expect(state.error).toBe("数据源 99999 不存在");
  });

  it("sendMessage 流式网络异常时兜底为错误消息", async () => {
    chatApi.sendMessageStream.mockRejectedValue(new Error("连接中断"));
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("查询", true);

    const state = useChatStore.getState();
    expect(state.messages[1].isError).toBe(true);
    expect(state.messages[1].content).toBe("连接中断");
    expect(state.loading).toBe(false);
  });

  it("流式 meta 事件回填消息 intent（MEDIUM#7）", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onMeta?.("chitchat");
      handlers.onDone?.({ tokensUsed: 0, cost: 0 });
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("你好", true);

    expect(useChatStore.getState().messages[1].intent).toBe("chitchat");
  });

  it("流式 meta 事件回填多轮意图 refine（Phase C 收窄扩展到完整意图集）", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onMeta?.("refine");
      handlers.onDone?.({ tokensUsed: 0, cost: 0 });
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("按数量升序排列", true);

    expect(useChatStore.getState().messages[1].intent).toBe("refine");
  });

  it("流式 plan 事件回填 ReAct 查询计划（Phase E）", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onMeta?.("query");
      handlers.onPlan?.({
        target: "各供应商的收货数量汇总",
        selectedClasses: ["PRECEIPT"],
        selectedProperties: ["NAME", "QTY"],
        aggregations: [{ function: "SUM", property: "QTY", alias: "TOTAL_QTY" }],
        groupBy: ["NAME"],
        conditions: [],
        joins: [],
        sortBy: [{ property: "NAME", direction: "asc" }],
        rowLimit: 10,
      });
      handlers.onDone?.({ tokensUsed: 45, cost: 0.00006, modelName: "deepseek-chat" });
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("各供应商的收货数量汇总", true);

    const assistant = useChatStore.getState().messages[1];
    expect(assistant.queryPlan?.target).toBe("各供应商的收货数量汇总");
    expect(assistant.queryPlan?.selectedClasses).toEqual(["PRECEIPT"]);
    expect(assistant.queryPlan?.aggregations).toEqual([
      { function: "SUM", property: "QTY", alias: "TOTAL_QTY" },
    ]);
    expect(assistant.isStreaming).toBe(false);
  });

  it("流式多步事件建立并推进 steps 状态（multi_step_plan/step_plan/step_result/done）", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onStepPlanOverview?.([
        { stepIndex: 0, description: "2024 销售额", subQuestion: "2024年销售额", aggregationOnly: false },
        { stepIndex: 1, description: "2025 销售额", subQuestion: "2025年销售额", aggregationOnly: false },
        { stepIndex: 2, description: "对比", subQuestion: "汇总", aggregationOnly: true },
      ]);
      handlers.onStepPlan?.({ stepIndex: 0, description: "2024 销售额", subQuestion: "2024年销售额" });
      handlers.onStepResult?.({ stepIndex: 0, description: "2024 销售额", subQuestion: "2024年销售额", sql: "SELECT 1", summary: "1000" });
      handlers.onStepPlan?.({ stepIndex: 1, description: "2025 销售额", subQuestion: "2025年销售额" });
      handlers.onStepResult?.({ stepIndex: 1, description: "2025 销售额", subQuestion: "2025年销售额", sql: "SELECT 2", summary: "1200" });
      handlers.onStepPlan?.({ stepIndex: 2, description: "对比", subQuestion: "汇总" });
      handlers.onToken?.("增长 20%。");
      handlers.onDone?.({ tokensUsed: 45, cost: 0.00006, modelName: "deepseek-chat" });
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("分步查询并对比", true);

    const assistant = useChatStore.getState().messages[1];
    expect(assistant.steps).toHaveLength(3);
    expect(assistant.steps?.[0]).toMatchObject({ status: "done", sql: "SELECT 1" });
    expect(assistant.steps?.[1]).toMatchObject({ status: "done", sql: "SELECT 2" });
    // 汇总步骤无 step_result，靠 done 收尾标记为已完成
    expect(assistant.steps?.[2]).toMatchObject({ status: "done", aggregationOnly: true });
    expect(assistant.currentStepIndex).toBe(2);
    expect(assistant.isStreaming).toBe(false);
  });

  it("流式 step_result 携带的每步图表回填到 step（多步每步出图）", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onStepPlanOverview?.([
        { stepIndex: 0, description: "各供应商收货量", subQuestion: "各供应商的收货量", aggregationOnly: false },
      ]);
      handlers.onStepResult?.({
        stepIndex: 0,
        description: "各供应商收货量",
        subQuestion: "各供应商的收货量",
        sql: "SELECT 1",
        summary: null,
        chartType: "hbar",
        chartOption: { series: [{ type: "bar" }] },
      });
      handlers.onDone?.({ tokensUsed: 15, cost: 0.00002 });
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("各供应商的收货量", true);

    const step = useChatStore.getState().messages[1].steps?.[0];
    expect(step?.chartType).toBe("hbar");
    expect(step?.chartOption).toEqual({ series: [{ type: "bar" }] });
  });

  it("流结束且无 done/error 帧时 loading 与 isStreaming 兜底复位（HIGH#3）", async () => {
    // 模拟流式端点成功返回但既无 done 也无 error 帧
    chatApi.sendMessageStream.mockResolvedValue(undefined);
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("查询", true);

    const state = useChatStore.getState();
    expect(state.loading).toBe(false);
    expect(state.messages[1].isStreaming).toBe(false);
  });

  it("error 事件后再抛异常时保留具体错误文案（LOW#8）", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onError?.("数据源 99999 不存在");
      throw new Error("连接中断");
    });
    useChatStore.getState().setDatasourceId(1);
    await useChatStore.getState().sendMessage("查询", true);

    const state = useChatStore.getState();
    expect(state.messages[1].content).toBe("数据源 99999 不存在");
    expect(state.messages[1].isError).toBe(true);
    expect(state.loading).toBe(false);
  });

  it("未选择数据源时发送返回错误且不调 API", async () => {
    await useChatStore.getState().sendMessage("查询");
    expect(chatApi.sendMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().error).toBe("请先选择数据源");
  });

  it("resetSession 清空消息并更换会话 id", () => {
    const oldSession = useChatStore.getState().sessionId;
    useChatStore.getState().addMessage({ id: "1", role: "user", content: "x", timestamp: 1 });
    useChatStore.getState().resetSession();
    const state = useChatStore.getState();
    expect(state.messages).toHaveLength(0);
    expect(state.sessionId).not.toBe(oldSession);
    expect(state.error).toBeNull();
  });

  // =========================================================================
  // 历史会话面板（feat-chat-history-panel）
  // =========================================================================

  it("loadSessions 填充 sessions 数组并清除 sessionsLoading", async () => {
    const fakeSessions: ChatSession[] = [
      {
        sessionId: "s1",
        firstTime: "2026-01-01T00:00:00Z",
        lastTime: "2026-01-01T01:00:00Z",
        messageCount: 2,
        lastQuestion: "A 的销售",
        lastAnswerPreview: "查询结果",
      },
      {
        sessionId: "s2",
        firstTime: "2026-01-02T00:00:00Z",
        lastTime: "2026-01-02T01:00:00Z",
        messageCount: 4,
        lastQuestion: "B 的库存",
        lastAnswerPreview: "库存结果",
      },
    ];
    historyApi.listChatSessions.mockResolvedValue(fakeSessions);

    await useChatStore.getState().loadSessions();

    const state = useChatStore.getState();
    expect(state.sessions).toEqual(fakeSessions);
    expect(state.sessionsLoading).toBe(false);
    expect(state.sessionsError).toBeNull();
    expect(historyApi.listChatSessions).toHaveBeenCalledTimes(1);
  });

  it("loadSessions 失败时设置 sessionsError 并保留 sessionsLoading=false", async () => {
    historyApi.listChatSessions.mockRejectedValue(new Error("网络异常"));

    await useChatStore.getState().loadSessions();

    const state = useChatStore.getState();
    expect(state.sessionsError).toBe("网络异常");
    expect(state.sessionsLoading).toBe(false);
    expect(state.sessions).toEqual([]);
  });

  it("loadSessionMessages 替换 messages 并更新 sessionId", async () => {
    // 预设当前消息
    useChatStore.setState({ messages: [{ id: "old", role: "user", content: "old", timestamp: 1 }] });

    const historyMessages: ChatMessageRead[] = [
      { id: 1, role: "user", content: "历史 Q1", question: "历史 Q1", sql: null, createdTime: "2026-01-01T00:00:00Z", interrupted: false },
      { id: 2, role: "assistant", content: "历史 A1", question: null, sql: "SELECT 1", createdTime: "2026-01-01T00:01:00Z", interrupted: false },
    ];
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "s-history",
      messages: historyMessages,
    });

    await useChatStore.getState().loadSessionMessages("s-history");

    const state = useChatStore.getState();
    expect(state.sessionId).toBe("s-history");
    expect(state.messages).toHaveLength(2);
    expect(state.messages[0].role).toBe("user");
    expect(state.messages[0].content).toBe("历史 Q1");
    expect(state.messages[1].role).toBe("assistant");
    expect(state.messages[1].sql).toBe("SELECT 1");
    // 不残留旧的"old"消息
    expect(state.messages.find((m) => m.id === "old")).toBeUndefined();
    // 错误清空
    expect(state.error).toBeNull();
  });

  it("loadSessionMessages 透传 interrupted（H4 断连兜底写入的半截回答）", async () => {
    const historyMessages: ChatMessageRead[] = [
      { id: 1, role: "user", content: "历史 Q1", question: "历史 Q1", sql: null, createdTime: "2026-01-01T00:00:00Z", interrupted: false },
      { id: 2, role: "assistant", content: "半截回答", question: null, sql: "SELECT 1", createdTime: "2026-01-01T00:01:00Z", interrupted: true },
      { id: 3, role: "assistant", content: "完整回答", question: null, sql: null, createdTime: "2026-01-01T00:02:00Z", interrupted: false },
    ];
    historyApi.loadSessionMessages.mockResolvedValue({ sessionId: "s-history", messages: historyMessages });

    await useChatStore.getState().loadSessionMessages("s-history");

    const messages = useChatStore.getState().messages;
    expect(messages[1].interrupted).toBe(true);
    expect(messages[2].interrupted).toBe(false);
  });

  it("loadSessionMessages 恢复图表字段（0105：历史回放也能出图）", async () => {
    const option = { columns: ["地区"], rows: [{ 地区: "华北" }] };
    const historyMessages: ChatMessageRead[] = [
      { id: 1, role: "user", content: "历史 Q1", question: "历史 Q1", sql: null, createdTime: "2026-01-01T00:00:00Z", interrupted: false },
      { id: 2, role: "assistant", content: "历史 A1", question: null, sql: "SELECT 1", createdTime: "2026-01-01T00:01:00Z", interrupted: false, chartType: "table", chartOption: option },
    ];
    historyApi.loadSessionMessages.mockResolvedValue({ sessionId: "s-history", messages: historyMessages });

    await useChatStore.getState().loadSessionMessages("s-history");

    const assistant = useChatStore.getState().messages[1];
    expect(assistant.chartType).toBe("table");
    expect(assistant.chartOption).toEqual(option);
  });

  it("loadSessionMessages 把白名单不认识的 chartType 收窄为 null", async () => {
    // 落库的 kind 可能来自更早版本的后端；未知类型不能流进渲染层
    const historyMessages: ChatMessageRead[] = [
      { id: 1, role: "assistant", content: "A", question: null, sql: null, createdTime: "2026-01-01T00:01:00Z", interrupted: false, chartType: "sankey-3d", chartOption: {} },
    ];
    historyApi.loadSessionMessages.mockResolvedValue({ sessionId: "s-history", messages: historyMessages });

    await useChatStore.getState().loadSessionMessages("s-history");

    const assistant = useChatStore.getState().messages[0];
    expect(assistant.chartType).toBeNull();
  });

  it("loadSessionMessages 对存量行（无图表字段）给 null 而不是 undefined", async () => {
    const historyMessages: ChatMessageRead[] = [
      { id: 1, role: "assistant", content: "A", question: null, sql: null, createdTime: "2026-01-01T00:01:00Z", interrupted: false },
    ];
    historyApi.loadSessionMessages.mockResolvedValue({ sessionId: "s-history", messages: historyMessages });

    await useChatStore.getState().loadSessionMessages("s-history");

    const assistant = useChatStore.getState().messages[0];
    expect(assistant.chartType).toBeNull();
    expect(assistant.chartOption).toBeNull();
  });

  it("loadSessionMessages 不影响 datasourceId/selectedModelId", async () => {
    useChatStore.setState({ datasourceId: 1, selectedModelId: 2 });
    historyApi.loadSessionMessages.mockResolvedValue({ sessionId: "s-history", messages: [] });

    await useChatStore.getState().loadSessionMessages("s-history");

    const state = useChatStore.getState();
    expect(state.datasourceId).toBe(1);
    expect(state.selectedModelId).toBe(2);
  });

  it("deleteSession 从 sessions 不可变移除指定项", async () => {
    const initial: ChatSession[] = [
      { sessionId: "s1", firstTime: "2026-01-01T00:00:00Z", lastTime: "2026-01-01T01:00:00Z", messageCount: 2, lastQuestion: "a", lastAnswerPreview: null },
      { sessionId: "s2", firstTime: "2026-01-02T00:00:00Z", lastTime: "2026-01-02T01:00:00Z", messageCount: 2, lastQuestion: "b", lastAnswerPreview: null },
      { sessionId: "s3", firstTime: "2026-01-03T00:00:00Z", lastTime: "2026-01-03T01:00:00Z", messageCount: 2, lastQuestion: "c", lastAnswerPreview: null },
    ];
    historyApi.deleteSessionHistory.mockResolvedValue(undefined);
    useChatStore.setState({ sessions: initial });

    await useChatStore.getState().deleteSession("s2");

    const state = useChatStore.getState();
    expect(state.sessions).toHaveLength(2);
    expect(state.sessions.map((s) => s.sessionId)).toEqual(["s1", "s3"]);
    // 不可变：引用不等
    expect(state.sessions).not.toBe(initial);
    // s2 的原引用也不在结果里
    expect(state.sessions).not.toContain(initial[1]);
    expect(historyApi.deleteSessionHistory).toHaveBeenCalledWith("s2");
  });

  it("deleteSession 删除当前会话时清空 messages 与 sessionId", async () => {
    historyApi.deleteSessionHistory.mockResolvedValue(undefined);
    useChatStore.setState({
      sessionId: "current",
      messages: [{ id: "x", role: "user", content: "x", timestamp: 1 }],
      sessions: [
        { sessionId: "current", firstTime: "", lastTime: "", messageCount: 1, lastQuestion: null, lastAnswerPreview: null },
      ],
    });

    await useChatStore.getState().deleteSession("current");

    const state = useChatStore.getState();
    expect(state.messages).toEqual([]);
    expect(state.sessionId).not.toBe("current");
    expect(state.sessions).toHaveLength(0);
  });

  it("deleteSession 删除非当前会话时保留 messages 与 sessionId", async () => {
    historyApi.deleteSessionHistory.mockResolvedValue(undefined);
    useChatStore.setState({
      sessionId: "current",
      messages: [{ id: "x", role: "user", content: "x", timestamp: 1 }],
      sessions: [
        { sessionId: "current", firstTime: "", lastTime: "", messageCount: 1, lastQuestion: null, lastAnswerPreview: null },
        { sessionId: "other", firstTime: "", lastTime: "", messageCount: 1, lastQuestion: null, lastAnswerPreview: null },
      ],
    });

    await useChatStore.getState().deleteSession("other");

    const state = useChatStore.getState();
    expect(state.messages).toHaveLength(1);
    expect(state.sessionId).toBe("current");
    expect(state.sessions.map((s) => s.sessionId)).toEqual(["current"]);
  });

  it("toggleHistoryPanel 翻转 historyPanelOpen 并写入 localStorage", () => {
    useChatStore.setState({ historyPanelOpen: false });

    useChatStore.getState().toggleHistoryPanel();
    expect(useChatStore.getState().historyPanelOpen).toBe(true);

    useChatStore.getState().toggleHistoryPanel();
    expect(useChatStore.getState().historyPanelOpen).toBe(false);

    // 两次写入都触发（每次切换都持久化）
    expect(persist.writeHistoryPanelOpen).toHaveBeenCalled();
  });

  it("setHistoryPanelOpen 强制设定并写入 localStorage", () => {
    useChatStore.getState().setHistoryPanelOpen(true);
    expect(useChatStore.getState().historyPanelOpen).toBe(true);
    expect(persist.writeHistoryPanelOpen).toHaveBeenCalled();
  });

  // 注：原先这里有两个用例自称验证「初始化从 localStorage 恢复 historyPanelOpen /
  // 有 lastSessionId 时自动恢复历史」，实际都只是手工调 setState / loadSessionMessages
  // 来「模拟 hydration 后行为」—— 断言的东西与描述的路径无关，模块级 hydration
  // 从来没有被覆盖过（这也是「会话指针恒为 null」能长期隐身的原因）。
  // 真用例已迁到 chatStoreHydration.test.ts（那里用 vi.resetModules + 动态 import
  // 真的让模块重新初始化；放在本文件会污染后续用例手上的 store 实例）。

  it("loadSessionMessages 失败时写入 sessionsError（不影响 chat 区域的 error）", async () => {
    historyApi.loadSessionMessages.mockRejectedValue(new Error("404 找不到会话"));
    const before = useChatStore.getState().error;

    await useChatStore.getState().loadSessionMessages("s-bad");

    const state = useChatStore.getState();
    // 加载错误单独存于 sessionsError（供面板 Alert 渲染）
    expect(state.sessionsError).toBe("404 找不到会话");
    // 不污染 chat 区 error
    expect(state.error).toBe(before);
  });
});

// ---------------------------------------------------------------------------
// v3.1 MB3 M-3：假设跨轮不串（session 级取数 → turn 级挂载）
// ---------------------------------------------------------------------------

describe("chatStore 假设跨轮不串（M-3）", () => {
  const hypothesis = (id: number, turnQuestion: string | null) => ({
    id,
    statement: `可能原因 ${id}`,
    driver: null,
    verificationSql: "SELECT 1",
    turnQuestion,
    createdTime: null,
  });

  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
    chatApi.fetchHypotheses.mockResolvedValue([]);
    chatApi.sendMessage.mockResolvedValue({
      answer: "查询完成",
      intent: "query",
      sql: "SELECT 1",
      chartType: null,
      chartOption: null,
      data: [],
      tokensUsed: 10,
      cost: 0,
      modelName: "m",
    });
    useChatStore.getState().setDatasourceId(1);
  });

  it("第 1 轮假设不挂到第 2 轮回答下方（turnQuestion 过滤）", async () => {
    // 第 1 轮：后端返回 turnQuestion === "Q1" 的假设
    chatApi.fetchHypotheses.mockResolvedValue([hypothesis(1, "Q1")]);
    await useChatStore.getState().sendMessage("Q1");
    expect(useChatStore.getState().messages[1].hypotheses).toHaveLength(1);

    // 第 2 轮：GET 仍返回同一 session 级列表（端点无 turn 维度）
    await useChatStore.getState().sendMessage("Q2");
    const second = useChatStore.getState().messages[3];
    expect(second.role).toBe("assistant");
    expect(second.content).toBe("查询完成");
    // MessageItem 渲染条件：hypotheses && hypotheses.length → 空数组/空值均不渲染
    expect(second.hypotheses ?? []).toHaveLength(0);
  });

  it("第 2 轮有本轮假设时只挂本轮（不混入上一轮）", async () => {
    chatApi.fetchHypotheses.mockResolvedValue([hypothesis(1, "Q1")]);
    await useChatStore.getState().sendMessage("Q1");

    chatApi.fetchHypotheses.mockResolvedValue([
      hypothesis(1, "Q1"),
      hypothesis(2, "Q2"),
    ]);
    await useChatStore.getState().sendMessage("Q2");
    const second = useChatStore.getState().messages[3];
    expect(second.hypotheses?.map((h) => h.id)).toEqual([2]);
  });

  it("turnQuestion 为 null 的假设不挂到任何轮次", async () => {
    chatApi.fetchHypotheses.mockResolvedValue([hypothesis(1, null)]);
    await useChatStore.getState().sendMessage("Q1");
    expect(useChatStore.getState().messages[1].hypotheses ?? []).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Task 7（可视化输出策略）：tableOption / visualRationale 在 store 各接入点的回填
// ---------------------------------------------------------------------------

describe("chatStore 两个新字段回填（0107）", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
    useChatStore.getState().setDatasourceId(1);
  });

  it("多步汇总消息最终带出 SUMMARY_TEXT_ONLY 依据（done 帧收窄并 patch 进消息）", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onStepPlanOverview?.([
        { stepIndex: 0, description: "2024 销售额", subQuestion: "2024年销售额", aggregationOnly: false },
        { stepIndex: 1, description: "对比", subQuestion: "汇总", aggregationOnly: true },
      ]);
      handlers.onStepPlan?.({ stepIndex: 0, description: "2024 销售额", subQuestion: "2024年销售额" });
      handlers.onStepResult?.({ stepIndex: 0, description: "2024 销售额", subQuestion: "2024年销售额", sql: "SELECT 1", summary: "1000" });
      handlers.onStepPlan?.({ stepIndex: 1, description: "对比", subQuestion: "汇总" });
      handlers.onToken?.("增长 20%。");
      handlers.onDone?.({
        tokensUsed: 45,
        cost: 0.00006,
        modelName: "deepseek-chat",
        visualRationale: { code: "SUMMARY_TEXT_ONLY", params: {} },
      });
    });
    await useChatStore.getState().sendMessage("分步查询并对比", true);

    const assistant = useChatStore.getState().messages[1];
    expect(assistant.visualRationale).toEqual({ code: "SUMMARY_TEXT_ONLY", params: {} });
    expect(assistant.visualRationale?.code).toBe("SUMMARY_TEXT_ONLY");
    // 数据步自己的依据在 step 上，不在顶层
    expect(assistant.steps?.[0].visualRationale).toBeNull();
  });

  it("单步 done 帧不带 rationale 时不清掉 chart 事件已回填的依据", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onChart?.({
        chartType: "donut",
        chartOption: { series: [] },
        tableOption: null,
        visualRationale: { code: "R02_SHARE_DONUT", params: { rows: 42 } },
        data: [],
      });
      handlers.onDone?.({ tokensUsed: 0, cost: 0 });
    });
    await useChatStore.getState().sendMessage("各品类占比", true);

    const assistant = useChatStore.getState().messages[1];
    expect(assistant.visualRationale).toEqual({ code: "R02_SHARE_DONUT", params: { rows: 42 } });
  });

  it("5 个 store 接入点对同一份负载回填相同结果（共享 fixture 循环）", async () => {
    const TABLE = { columns: ["NAME"], rows: [{ NAME: "A" }], truncated: true };
    const RATIONALE = { code: "R02_SHARE_DONUT", params: { rows: 42 } };

    const sites: Array<{
      name: string;
      run: () => Promise<{ tableOption?: unknown; visualRationale?: unknown }>;
    }> = [
      {
        name: "历史回放（toChatMessage）",
        run: async () => {
          resetStore();
          historyApi.loadSessionMessages.mockResolvedValue({
            sessionId: "s-h",
            messages: [
              {
                id: 1,
                role: "assistant",
                content: "答",
                question: null,
                sql: null,
                createdTime: "2026-01-01T10:00:00Z",
                interrupted: false,
                tableOption: { columns: ["NAME"], rows: [{ NAME: "A" }], truncated: true },
                visualRationale: { code: "R02_SHARE_DONUT", params: { rows: 42 } },
              },
            ],
          });
          persist.readLastSessionId.mockImplementation((ch) => (ch === "chat" ? "s-h" : null));
          await useChatStore.getState().enterChannel("chat");
          const m = useChatStore.getState().messages[0];
          return { tableOption: m.tableOption, visualRationale: m.visualRationale };
        },
      },
      {
        name: "SSE chart → 消息",
        run: async () => {
          resetStore();
          useChatStore.getState().setDatasourceId(1);
          chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
            handlers.onChart?.({ chartType: "donut", chartOption: {}, tableOption: TABLE, visualRationale: RATIONALE, data: [] });
            handlers.onDone?.({ tokensUsed: 0, cost: 0 });
          });
          await useChatStore.getState().sendMessage("q", true);
          const m = useChatStore.getState().messages[1];
          return { tableOption: m.tableOption, visualRationale: m.visualRationale };
        },
      },
      {
        name: "SSE step_result → 步骤",
        run: async () => {
          resetStore();
          useChatStore.getState().setDatasourceId(1);
          chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
            handlers.onStepPlanOverview?.([
              { stepIndex: 0, description: "d", subQuestion: "q", aggregationOnly: false },
            ]);
            handlers.onStepResult?.({ stepIndex: 0, description: "d", subQuestion: "q", sql: "SELECT 1", summary: null, tableOption: TABLE, visualRationale: RATIONALE });
            handlers.onDone?.({ tokensUsed: 0, cost: 0 });
          });
          await useChatStore.getState().sendMessage("q", true);
          const step = useChatStore.getState().messages[1].steps?.[0];
          return { tableOption: step?.tableOption, visualRationale: step?.visualRationale };
        },
      },
      {
        name: "非流式响应 → 消息",
        run: async () => {
          resetStore();
          useChatStore.getState().setDatasourceId(1);
          chatApi.sendMessage.mockResolvedValue({
            answer: "查询完成",
            intent: "query",
            tableOption: TABLE,
            visualRationale: RATIONALE,
            tokensUsed: 0,
            cost: 0,
          });
          await useChatStore.getState().sendMessage("q");
          const m = useChatStore.getState().messages[1];
          return { tableOption: m.tableOption, visualRationale: m.visualRationale };
        },
      },
      {
        name: "非流式 steps[] → 步骤",
        run: async () => {
          resetStore();
          useChatStore.getState().setDatasourceId(1);
          chatApi.sendMessage.mockResolvedValue({
            answer: "查询完成",
            intent: "multi_step",
            tokensUsed: 0,
            cost: 0,
            steps: [
              {
                stepIndex: 0,
                description: "d",
                subQuestion: "q",
                sql: "SELECT 1",
                summary: null,
                error: null,
                tableOption: TABLE,
                visualRationale: RATIONALE,
              },
            ],
          });
          await useChatStore.getState().sendMessage("q");
          const step = useChatStore.getState().messages[1].steps?.[0];
          return { tableOption: step?.tableOption, visualRationale: step?.visualRationale };
        },
      },
    ];

    const results: Array<{ tableOption?: unknown; visualRationale?: unknown }> = [];
    for (const site of sites) {
      results.push(await site.run());
    }

    // 5 处全同一份收窄结果 —— 复制断言会在加第 6 处时漏掉，故这里只循环
    for (const [i, r] of results.entries()) {
      expect(r, sites[i].name).toMatchObject({
        tableOption: TABLE,
        visualRationale: RATIONALE,
      });
    }
  });
});

// ===== Task 9 fix round 1：续跑必须写「被点的那条消息」，而不是「最后一条」 =====
// 复现：多步跑到某步失败 → 追问一句（loading 回落 false）→ 上滚点**旧卡片**的续跑。
// 旧实现把该 run 的 step 事件全写进最新那条消息 ⇒ 被点的卡片纹丝不动、最新消息被污染。
describe("chatStore resumeRun 定向写入", () => {
  const EARLY_STEPS = [
    {
      stepIndex: 0,
      description: "旧步0",
      subQuestion: "q0",
      aggregationOnly: false,
      status: "done" as const,
      sql: "SELECT 0",
      summary: null,
      error: null,
      runId: "r-old",
    },
    {
      stepIndex: 1,
      description: "旧步1",
      subQuestion: "q1",
      aggregationOnly: false,
      status: "error" as const,
      sql: null,
      summary: null,
      error: "boom",
      runId: "r-old",
    },
  ];

  function makeMessages(): { early: ChatMessage; newest: ChatMessage } {
    const early: ChatMessage = {
      id: "m-early",
      role: "assistant",
      content: "旧的多步回答",
      timestamp: 1,
      steps: EARLY_STEPS,
    };
    const newest: ChatMessage = {
      id: "m-new",
      role: "assistant",
      content: "最新的追问回答",
      timestamp: 2,
      sql: "SELECT n",
      steps: [
        {
          stepIndex: 0,
          description: "新步0",
          subQuestion: "qn",
          aggregationOnly: false,
          status: "done",
          sql: "SELECT n",
          summary: null,
          error: null,
          runId: "r-new",
        },
      ],
    };
    return { early, newest };
  }

  it("对较早那条续跑只更新它，最新那条逐字段不动（引用相等）", async () => {
    // Arrange
    const { early, newest } = makeMessages();
    useChatStore.setState({ messages: [early, newest], loading: false });
    chatApi.resumeMultiStepRun.mockImplementation(
      async (_runId: string, _from: number, handlers: StreamEventHandlers) => {
        handlers.onStepPlan?.({ stepIndex: 1, description: "旧步1", subQuestion: "q1" });
        handlers.onStepResult?.({
          stepIndex: 1,
          description: "旧步1",
          subQuestion: "q1",
          sql: "SELECT 1",
          summary: "ok",
          error: null,
        });
      }
    );

    // Act
    await useChatStore.getState().resumeRun("r-old", 1, "m-early");

    // Assert —— ① 被点的那条消息被推进（running → done，并回填 sql）
    const msgs = useChatStore.getState().messages;
    expect(msgs[0].steps?.[1].status).toBe("done");
    expect(msgs[0].steps?.[1].sql).toBe("SELECT 1");
    // 未被续跑的那一步不动
    expect(msgs[0].steps?.[0].status).toBe("done");
    // ② 最新那条消息逐个字段未被改动（引用相等 = 不可变更新只碰目标）
    expect(msgs[1]).toBe(newest);
    // ③ 请求带的是被点的 runId 与起始步号
    expect(chatApi.resumeMultiStepRun).toHaveBeenCalledWith("r-old", 1, expect.anything());
  });
});
