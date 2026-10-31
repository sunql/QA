import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { readFileSync } from "node:fs";

// ResumeRunButton 渲染即调 useLlmModels（本文件里任何带 steps 的消息都会走到）。
// 默认值必须在**模块级**给全：只在本文件末尾那个 describe 的 beforeEach 里设的话，
// 别的 describe（如「续跑失败横幅」）先跑就会拿到 undefined，ResumeRunButton
// 解构 data 直接崩——那不是被测行为，是 mock 漏配。
const useLlmModelsMock = vi.fn(() => ({
  data: [
    { id: 1, modelName: "deepseek-chat", provider: "OPENAI_COMPATIBLE_PROXY" },
    { id: 2, modelName: "Qwen3.8-27B-4bit", provider: "OLLAMA" },
  ],
  loading: false,
  error: null,
  reload: vi.fn(),
}));
vi.mock("../hooks/useLlmModels", () => ({
  useLlmModels: () => useLlmModelsMock(),
}));

import MessageItem from "../components/chat/MessageItem";
import { useChatStore } from "../stores/chatStore";
import { useThemeStore } from "../stores/themeStore";
import { DARK_TOKEN, LIGHT_TOKEN } from "../theme/tokens";
import type { ChatMessage } from "../types/chat";

// ChartRenderer 用替身：本文件测的是「何时渲染图表/骨架」的门控与时序，
// 不是 ChartRenderer 内部（那在 ChartRenderer.test.tsx）。替身让骨架与图表
// 的出现与否可以断言 testid，而不必在 jsdom 里实例化 ECharts。
vi.mock("../components/chat/ChartRenderer", () => ({
  __esModule: true,
  default: (props: { chartType?: string | null }) => (
    <div data-testid="chart-renderer" data-chart-type={props.chartType ?? ""} />
  ),
}));

function renderItem(overrides: Partial<ChatMessage> = {}) {
  const message: ChatMessage = {
    id: "m-1",
    role: "assistant",
    content: "纯文本回答",
    timestamp: 1,
    ...overrides,
  };
  return render(
    <ConfigProvider locale={zhCN}>
      <MessageItem message={message} />
    </ConfigProvider>
  );
}

describe("MessageItem 消息渲染", () => {
  it("纯文本回答原样渲染", () => {
    renderItem({ content: "一共 2 条记录。" });
    expect(screen.getByText("一共 2 条记录。")).toBeInTheDocument();
  });

  it("渲染 Markdown 加粗文本", () => {
    const { container } = renderItem({
      content: "排名第一的是**供应商甲**。",
    });
    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong?.textContent).toBe("供应商甲");
  });

  it("渲染有序列表（编号 + 加粗）", () => {
    const { container } = renderItem({
      content: "采购订单数量排行如下：\n\n1. **甲** — 13,415 个\n2. **乙** — 10,492 个",
    });
    const list = container.querySelector("ol");
    expect(list).not.toBeNull();
    const items = container.querySelectorAll("li");
    expect(items.length).toBe(2);
    expect(items[0].textContent).toContain("甲");
    expect(items[1].textContent).toContain("乙");
  });

  it("渲染 Markdown 表格（GFM）", () => {
    const { container } = renderItem({
      content: "| 供应商 | 数量 |\n| --- | --- |\n| 甲 | 100 |",
    });
    const tbody = container.querySelector("tbody");
    expect(tbody).not.toBeNull();
    expect(within(tbody as HTMLElement).getByText("100")).toBeInTheDocument();
  });

  it("渲染行内代码与围栏代码块", () => {
    const { container } = renderItem({
      content: "执行 `SELECT 1` 获得结果：\n\n```sql\nSELECT COUNT(*) FROM t;\n```",
    });
    const inlineCode = container.querySelector(":not(pre) > code");
    expect(inlineCode).not.toBeNull();
    expect(inlineCode?.textContent).toBe("SELECT 1");
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toContain("SELECT COUNT(*) FROM t;");
  });

  it("渲染链接与引用", () => {
    const { container } = renderItem({
      content: "参考 [文档](https://example.com)。\n\n> 提示：仅供只读查询。",
    });
    const link = container.querySelector("a[href='https://example.com']");
    expect(link).not.toBeNull();
    expect(link?.textContent).toBe("文档");
    const blockquote = container.querySelector("blockquote");
    expect(blockquote).not.toBeNull();
    expect(blockquote?.textContent).toContain("仅供只读查询");
  });

  it("不渲染 raw HTML（XSS 安全）", () => {
    const { container } = renderItem({
      content: '文本 <script>alert("xss")</script> 和 <img src=x onerror=alert(1)>',
    });
    // 未启用 rehype-raw：<script>/<img> 不作为 DOM 渲染
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    // 原始 HTML 以文本形式呈现
    expect(screen.getByText(/文本/)).toBeInTheDocument();
  });

  it("流式输出期间保留打字光标且不做 Markdown 解析", () => {
    const { container } = renderItem({
      content: "回答中… 1. **供应商**",
      isStreaming: true,
    });
    // 打字光标保留
    expect(container.querySelector(".typing-cursor")).not.toBeNull();
    // 未解析为 <strong>（流式增量不做解析，避免闪烁）
    expect(container.querySelector("strong")).toBeNull();
    expect(container.querySelector("ol")).toBeNull();
  });

  it("用户消息保持纯文本", () => {
    renderItem({ role: "user", content: "各供应商收货量汇总" });
    expect(screen.getByText("各供应商收货量汇总")).toBeInTheDocument();
    expect(document.querySelector("strong")).toBeNull();
  });

  it("错误消息以 Alert 呈现，不做 Markdown 解析", () => {
    const { container } = renderItem({
      content: "服务不可用 **详情**",
      isError: true,
    });
    expect(screen.getByText("服务不可用 **详情**")).toBeInTheDocument();
    expect(container.querySelector(".ant-alert")).not.toBeNull();
    expect(container.querySelector("strong")).toBeNull();
  });

  it("错误消息含 errorDetail 时展示可折叠的校验失败详情", () => {
    renderItem({
      content: "无法生成通过校验的查询计划",
      isError: true,
      errorDetail: "类 PRECEIPTD 不存在；属性 收货数量 不存在",
    });
    expect(screen.getByText("无法生成通过校验的查询计划")).toBeInTheDocument();
    // 折叠标签存在；展开后可见具体差异
    fireEvent.click(screen.getByText("校验失败详情"));
    expect(screen.getByText("类 PRECEIPTD 不存在；属性 收货数量 不存在")).toBeInTheDocument();
  });

  it("错误消息无 errorDetail 时不渲染校验详情折叠面板", () => {
    renderItem({ content: "服务不可用", isError: true });
    expect(screen.queryByText("校验失败详情")).toBeNull();
  });

  it("已知 errorType 渲染可操作的差异化提示（真实文案，无裸 key）", () => {
    renderItem({
      content: "查询超时",
      isError: true,
      errorType: "query_timeout",
    });
    expect(
      screen.getByText("查询超时，可缩小时间范围或减少维度后重试。")
    ).toBeInTheDocument();
    // i18n 缺 key 时会回落成 key 本身 ⇒ 必须反向断言不出现裸 key
    expect(screen.queryByText(/messageItem\.errorHints\./)).toBeNull();
    expect(screen.queryByText(/query_timeout/)).toBeNull();
  });

  it("未知 errorType 不渲染提示、不透传原始枚举", () => {
    renderItem({
      content: "服务不可用",
      isError: true,
      errorType: "some_future_type",
    });
    expect(screen.getByText("服务不可用")).toBeInTheDocument();
    expect(screen.queryByText(/some_future_type/)).toBeNull();
    expect(screen.queryByText(/messageItem\.errorHints\./)).toBeNull();
  });

  it("原型链键名（constructor）不命中白名单、不崩溃", () => {
    renderItem({
      content: "服务不可用",
      isError: true,
      errorType: "constructor",
    });
    expect(screen.getByText("服务不可用")).toBeInTheDocument();
    expect(screen.queryByText(/messageItem\.errorHints\./)).toBeNull();
  });

  it("错误消息展示「添加到术语词典」入口", () => {
    renderItem({ content: "无法生成查询计划", isError: true });
    expect(screen.getByRole("button", { name: "添加到术语词典" })).toBeInTheDocument();
  });

  it("正常回答同时展示 Token/成本/模型标签", () => {
    renderItem({
      content: "查询完成。",
      tokensUsed: 45,
      cost: 0.00006,
      modelName: "deepseek-chat",
    });
    expect(screen.getByText("Tokens: 45")).toBeInTheDocument();
    expect(screen.getByText("成本: $0.000060")).toBeInTheDocument();
    expect(screen.getByText("模型: deepseek-chat")).toBeInTheDocument();
  });

  // =========================================================================
  // Phase E：ReAct 查询计划展示
  // =========================================================================

  it("助手消息展示可折叠查询计划（选表/选列/聚合/排序）", () => {
    renderItem({
      content: "查询完成。",
      queryPlan: {
        target: "各供应商的收货数量汇总",
        selectedClasses: ["PRECEIPT"],
        selectedProperties: ["NAME", "QTY"],
        aggregations: [{ function: "SUM", property: "QTY", alias: "TOTAL_QTY" }],
        groupBy: ["NAME"],
        conditions: [],
        joins: [],
        sortBy: [{ property: "NAME", direction: "asc" }],
        rowLimit: null,
      },
    });
    expect(screen.getByText("查看查询计划")).toBeInTheDocument();
    // 默认折叠（懒渲染）：展开后再断言计划内容
    fireEvent.click(screen.getByText("查看查询计划"));
    expect(screen.getByText("查询目标")).toBeInTheDocument();
    expect(screen.getByText("各供应商的收货数量汇总")).toBeInTheDocument();
    expect(screen.getByText("PRECEIPT")).toBeInTheDocument();
    expect(screen.getByText("SUM(QTY) AS TOTAL_QTY")).toBeInTheDocument();
    expect(screen.getByText("NAME ASC")).toBeInTheDocument();
  });

  it("流式输出期间隐藏查询计划（完成后展示）", () => {
    renderItem({
      content: "查询中…",
      isStreaming: true,
      queryPlan: {
        target: "各供应商的收货数量汇总",
        selectedClasses: ["PRECEIPT"],
        selectedProperties: ["NAME"],
        aggregations: [],
        groupBy: [],
        conditions: [],
        joins: [],
        sortBy: [],
        rowLimit: null,
      },
    });
    expect(screen.queryByText("查看查询计划")).toBeNull();
  });

  // =========================================================================
  // Phase 6.4：Agent 运行时响应卡片
  // =========================================================================

  it("消息含 agentRun 时渲染 AgentResponseCard", () => {
    renderItem({
      content: "供应商 **SUP000001** 风险等级：high。",
      intent: "agent_run",
      agentRun: {
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
      },
    });
    // AgentResponseCard 头部 agentName + tool Tag
    expect(screen.getByText("供应商风险 Agent")).toBeInTheDocument();
    expect(screen.getByText("supplier_risk")).toBeInTheDocument();
  });

  // =========================================================================
  // Phase 7 G4：未指名 Agent 语义路由建议卡片
  // =========================================================================

  it("消息含 suggestedAgent 时渲染 SuggestedAgentCard", () => {
    renderItem({
      content: "查询完成。",
      intent: "query",
      suggestedAgent: {
        recommendedAgentCode: "SUPPLIER_RISK_AGENT",
        confidence: 0.8,
        reason: "检测到风险评估类诉求，推荐风险健康度评估",
      },
    });
    expect(screen.getByText("建议使用 Agent")).toBeInTheDocument();
    expect(screen.getByText("SUPPLIER_RISK_AGENT")).toBeInTheDocument();
    expect(screen.getByText("置信度: 80%")).toBeInTheDocument();
  });

  it("无 suggestedAgent 时不渲染建议卡片", () => {
    renderItem({ content: "查询完成。", intent: "query" });
    expect(screen.queryByText("建议使用 Agent")).toBeNull();
  });

  // =========================================================================
  // Phase 8：SQL 预览使用只读 Monaco + 自定义复制按钮
  // =========================================================================

  describe("SQL 预览（Phase 8）", () => {
    const SAMPLE_SQL = "SELECT NAME FROM PRECEIPT WHERE QTY > 10";

    // jsdom 不实现 navigator.clipboard；通过 Object.defineProperty 注入 mock。
    let writeTextSpy: ReturnType<typeof vi.fn>;

    beforeEach(() => {
      writeTextSpy = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: writeTextSpy },
      });
    });

    afterEach(() => {
      // 恢复默认 navigator.clipboard（避免污染后续测试）
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: undefined,
        writable: true,
      });
    });

    it("含 SQL 的助手消息渲染 Monaco 只读编辑器与复制按钮", () => {
      renderItem({ content: "查询完成。", sql: SAMPLE_SQL });
      // Collapse 默认折叠，点击「查看 SQL」展开
      fireEvent.click(screen.getByText("查看 SQL"));
      const monaco = screen.getByTestId("monaco-mock");
      expect(monaco).toBeInTheDocument();
      expect(monaco.textContent).toBe(SAMPLE_SQL);
      expect(screen.getByTestId("sql-copy-button")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /复制/ })).toBeInTheDocument();
    });

    it("点击复制按钮触发 clipboard.writeText 并切换文案", async () => {
      renderItem({ content: "查询完成。", sql: SAMPLE_SQL });
      fireEvent.click(screen.getByText("查看 SQL"));
      const button = screen.getByTestId("sql-copy-button");
      fireEvent.click(button);
      await vi.waitFor(() => {
        expect(writeTextSpy).toHaveBeenCalledWith(SAMPLE_SQL);
      });
      // 点击后文案切换为「已复制」（短暂态）
      await vi.waitFor(() => {
        expect(screen.getByTestId("sql-copy-button").textContent).toMatch(/已复制/);
      });
    });

    it("clipboard 写入失败时降级不抛错（按钮文案保持「复制」）", async () => {
      writeTextSpy.mockRejectedValueOnce(new Error("permission denied"));
      renderItem({ content: "查询完成。", sql: SAMPLE_SQL });
      fireEvent.click(screen.getByText("查看 SQL"));
      const button = screen.getByTestId("sql-copy-button");
      // 不应冒泡到测试失败
      fireEvent.click(button);
      expect(writeTextSpy).toHaveBeenCalled();
      // 失败后文案不切换（仍是「复制」）
      expect(screen.getByTestId("sql-copy-button").textContent).toMatch(/^复制/);
    });
  });
});

// 卡片顺序（2026-10-31 用户反馈）：单步问题同时有 分析计划（MultiStepPlanCard）、
// 查询计划（QueryPlanCard）、SQL（SqlPreview）三张卡，用户要求固定为
// 分析计划 → 查询计划 → SQL 的阅读顺序。用 DOM 先后序断言（compareDocumentPosition），
// 不依赖组件内部实现 —— 面板标题文案是三张卡各自的稳定锚点。
describe("MessageItem 卡片顺序", () => {
  const PLAN_STEPS = [
    {
      stepIndex: 0,
      description: "供应商数量",
      subQuestion: "B019 供应商有多少个",
      aggregationOnly: false,
      status: "done" as const,
      sql: "SELECT count(*) FROM dim_supplier",
    },
  ];

  function assertBefore(earlier: Element, later: Element) {
    // Node.DOCUMENT_POSITION_FOLLOWING = 4：earlier 在 later 之前
    expect(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  }

  it("渲染顺序为 分析计划 → 查询计划 → SQL", () => {
    renderItem({
      content: "查询完成。",
      steps: PLAN_STEPS,
      queryPlan: {
        target: "供应商",
        selectedClasses: ["DIM_SUPPLIER"],
        selectedProperties: ["NAME"],
        aggregations: [],
        groupBy: [],
        conditions: [],
        joins: [],
        sortBy: [],
      } as never,
      sql: "SELECT count(*) FROM dim_supplier",
    });
    const planCard = screen.getByText("查看分析计划");
    const queryPlanCard = screen.getByText("查看查询计划");
    const sqlCard = screen.getByText("查看 SQL");
    assertBefore(planCard, queryPlanCard);
    assertBefore(queryPlanCard, sqlCard);
  });
});

describe("MessageItem 类召回诊断提示", () => {
  it("classRecall.truncated=true 渲染截断提示", () => {
    renderItem({
      content: "查询完成。",
      classRecall: { mode: "expanded", hitCount: 3, classCount: 30, truncated: true },
    });
    expect(screen.getByText(/已达上限（30 张）/)).toBeInTheDocument();
  });

  it("classRecall.mode=fallback 渲染降级提示", () => {
    renderItem({
      content: "查询完成。",
      classRecall: { mode: "fallback", hitCount: 0, classCount: 96, truncated: false },
    });
    expect(screen.getByText(/智能召回暂不可用/)).toBeInTheDocument();
  });

  it("正常召回（recall/expanded 未截断）不渲染提示", () => {
    renderItem({
      content: "查询完成。",
      classRecall: { mode: "recall", hitCount: 5, classCount: 7, truncated: false },
    });
    expect(screen.queryByText(/已达上限/)).not.toBeInTheDocument();
    expect(screen.queryByText(/智能召回暂不可用/)).not.toBeInTheDocument();
  });
});

describe("MessageItem 中断回答提示（H4 断连兜底）", () => {
  it("interrupted=true 渲染中断提示（内容可能是半截）", () => {
    renderItem({ content: "查询完成，", interrupted: true });
    expect(screen.getByText(/已中断/)).toBeInTheDocument();
  });

  it("interrupted 缺省/为 false 不渲染提示", () => {
    renderItem({ content: "查询完成。" });
    expect(screen.queryByText(/已中断/)).not.toBeInTheDocument();

    renderItem({ content: "查询完成。", interrupted: false });
    expect(screen.queryByText(/已中断/)).not.toBeInTheDocument();
  });
});

describe("MessageItem 续跑失败横幅", () => {
  it("resumeError 渲染横幅，且原正文与多步计划卡仍然在（不整块替换）", () => {
    // 缺陷对照：续跑失败曾写 `content: msg, isError: true` —— MessageItem 的
    // isError 分支会把正文（含计划卡）整块换成错误 Alert。改为独立横幅字段后，
    // 用户既知道失败原因，也保留原回答与「再点一次续跑」的入口。
    renderItem({
      content: "上一轮的有效回答",
      resumeError: "resume boom",
      steps: [
        {
          stepIndex: 1,
          description: "各供应商收货量",
          subQuestion: "各供应商的收货量是多少",
          aggregationOnly: false,
          status: "error",
          sql: null,
          summary: null,
          error: "boom",
          runId: "r-1",
        },
      ],
    });

    expect(screen.getByText(/续跑失败/)).toBeInTheDocument();
    expect(screen.getByText(/resume boom/)).toBeInTheDocument();
    // 原正文还在（isError 路径会把它换掉）
    expect(screen.getByText("上一轮的有效回答")).toBeInTheDocument();
    // 计划卡也还在 —— 展开后普通失败步的续跑按钮仍可点第二次
    fireEvent.click(screen.getByText("查看分析计划"));
    expect(screen.getByTestId("resume-run")).toBeInTheDocument();
  });

  it("没有 resumeError 时不渲染横幅", () => {
    renderItem({ content: "纯文本回答" });
    expect(screen.queryByText(/续跑失败/)).toBeNull();
  });
});

describe("MessageItem KPI 数据源不一致警告横幅", () => {
  it("已知 code 渲染警告横幅（白名单映射表）", () => {
    renderItem({
      content: "KPI 回答内容",
      kpiDatasourceCode: "KPI_DATASOURCE_MISMATCH",
    });
    // 白名单 code 命中，Alert 出现，本地 i18n 文案显示
    expect(screen.getByText(/KPI 指标绑定的数据源与你当前选择的数据源不一致/)).toBeInTheDocument();
  });

  it("未知 code 不渲染 Alert，也不冒泡裸 key（白名单制守卫）", () => {
    const { container } = renderItem({
      content: "回答内容",
      kpiDatasourceCode: "KPI_FUTURE_UNKNOWN",
    });
    // 白名单没有这个 code，Alert 不出现
    expect(container.querySelector(".ant-alert")).toBeNull();
    // 渲染输出里没有裸露的 i18n key
    expect(container.textContent).not.toContain("multiStep.");
  });
});

describe("MessageItem 消息级 visualRationale（Task 8，多步汇总）", () => {
  it("多步汇总消息渲染 SUMMARY_TEXT_ONLY 说明行（答案下方次要色）", () => {
    renderItem({
      content: "以上为各步骤的对比结论。",
      visualRationale: { code: "SUMMARY_TEXT_ONLY", params: {} },
    });
    expect(screen.getByText("汇总为文字结论，各步骤图表见上方")).toBeInTheDocument();
  });

  it("无 visualRationale 时不渲染说明行", () => {
    renderItem({ content: "查询完成。" });
    expect(screen.queryByText("汇总为文字结论，各步骤图表见上方")).toBeNull();
  });
});

/**
 * N4「换模型续跑」在 Chat 侧真正生效的那一跳。
 *
 * 断点就在本文件对应的组件里：`MessageItem` 给 `MultiStepPlanCard` 的 onResume
 * 回调原先只声明 `(runId, fromStepIndex)` 两个形参，于是 ResumeRunButton 传来的
 * 第 3 参 modelId 被**静默丢弃**——用户明明在 Popover 里选了模型，store 收到的
 * 仍是 undefined，续跑照 run 的原模型跑。
 *
 * 注意断言要落在这一层而不是 MultiStepPlanCard 层：那张卡只是把 prop 直传给
 * ResumeRunButton（MultiStepPlanCard.tsx:140），运行时第 3 参本来就流得过去，
 * 在那层写测试**回退修复后依然全绿**，抓不到本 bug（已实测确认）。
 *
 * 反向自检：把 onResume 的回调改回 2 参形参 ⇒ 下面两条都红。
 */
/** 本文件末尾那组用例会替掉 store 的 resumeRun；用完必须还原，别泄漏给别的用例。 */
const realResumeRun = useChatStore.getState().resumeRun;

describe("MessageItem 换模型续跑（N4 接缝）", () => {
  const FAILED_STEP = {
    stepIndex: 2,
    description: "统计各供应商收货量",
    subQuestion: "各供应商收货量是多少",
    aggregationOnly: false,
    status: "error" as const,
    error: "oMLX timeout",
    sql: null,
    summary: null,
    runId: "r-1",
  };

  beforeEach(() => {
    useChatStore.setState({ loading: false, selectedModelId: null } as any);
  });

  afterEach(() => {
    useChatStore.setState({ resumeRun: realResumeRun } as any);
  });

  /** 渲染一条带失败步的 assistant 消息，并展开分析计划面板。 */
  function renderFailedStepMessage() {
    renderItem({
      content: "续跑失败",
      originalModelId: 2,
      steps: [FAILED_STEP] as any,
    });
    fireEvent.click(screen.getByText("查看分析计划"));
  }

  it("选「原模型续跑」→ resumeRun 收到 modelId=null", async () => {
    const resumeRun = vi.fn();
    useChatStore.setState({ resumeRun } as any);
    renderFailedStepMessage();

    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-with-original"));
    fireEvent.click(screen.getByTestId("resume-with-original"));

    expect(resumeRun).toHaveBeenCalledWith("r-1", 2, "m-1", null);
  });

  it("选另一个模型续跑 → resumeRun 第 4 参必须带所选 modelId（本 bug 的守卫）", async () => {
    const resumeRun = vi.fn();
    useChatStore.setState({ resumeRun } as any);
    renderFailedStepMessage();

    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-model-select"));
    // 原模型是 Qwen(2)；换成 deepseek(1)
    fireEvent.mouseDown(document.querySelector(".ant-select-selector") as HTMLElement);
    fireEvent.click(await screen.findByText(/^deepseek-chat/));
    fireEvent.click(screen.getByTestId("resume-with-selected"));

    expect(resumeRun).toHaveBeenCalledWith("r-1", 2, "m-1", 1);
  });
});

describe("MessageItem 图表门控与卡片主题化", () => {
  afterEach(() => {
    useThemeStore.setState({ isDark: false });
  });

  /** ChartRenderer 替身渲染它时必然可见；「无图表」= 替身未挂载。 */
  const CHART_FIELDS = { chartType: "bar", chartOption: { series: [{ type: "bar" }] } } as const;

  /** 独立渲染助手消息（可 rerender，用于流式时序断言）。 */
  function renderMessage(props: Partial<ChatMessage>) {
    const message: ChatMessage = {
      id: "m-1",
      role: "assistant",
      content: "回答",
      timestamp: 1,
      ...props,
    };
    return render(
      <ConfigProvider locale={zhCN}>
        <MessageItem message={message} />
      </ConfigProvider>
    );
  }

  function rerenderMessage(
    rerender: (ui: React.ReactElement) => void,
    props: Partial<ChatMessage>
  ): void {
    const message: ChatMessage = {
      id: "m-1",
      role: "assistant",
      content: "回答",
      timestamp: 1,
      ...props,
    };
    rerender(
      <ConfigProvider locale={zhCN}>
        <MessageItem message={message} />
      </ConfigProvider>
    );
  }

  function cardBackground(): string | undefined {
    return document.querySelector<HTMLElement>(".ant-card")?.style.background;
  }

  /** jsdom 把十六进制颜色规范化为 rgb()，比较前对齐口径。 */
  function hexToRgb(hex: string): string {
    const value = hex.replace("#", "");
    const channels = [0, 2, 4].map((offset) => parseInt(value.slice(offset, offset + 2), 16));
    return `rgb(${channels.join(", ")})`;
  }

  // ===== 卡片背景语义 token 化（暗色下 #fafafa 上白色轴文字不可读的修复点）=====

  it("亮色：助手卡片背景 == LIGHT_TOKEN.colorChatAssistantBg", () => {
    renderMessage({ ...CHART_FIELDS });
    expect(cardBackground()).toBe(hexToRgb(LIGHT_TOKEN.colorChatAssistantBg));
  });

  it("亮色：用户卡片背景 == LIGHT_TOKEN.colorChatUserBg", () => {
    renderMessage({ role: "user", content: "问" });
    expect(cardBackground()).toBe(hexToRgb(LIGHT_TOKEN.colorChatUserBg));
  });

  it("暗色：助手/用户卡片分别切换为 DARK_TOKEN 的语义色", () => {
    useThemeStore.setState({ isDark: true });
    const { unmount } = renderMessage({ ...CHART_FIELDS });
    expect(cardBackground()).toBe(hexToRgb(DARK_TOKEN.colorChatAssistantBg));
    // querySelector 取的是文档里第一张卡：先卸载助手卡，再验用户卡
    unmount();

    renderMessage({ role: "user", content: "问" });
    expect(cardBackground()).toBe(hexToRgb(DARK_TOKEN.colorChatUserBg));
  });

  it("组件源码不再硬编码卡片背景字面量（防回归钉）", () => {
    // vitest 的 cwd 即 frontend/，相对路径最稳（import.meta.url 在转换后非 file scheme）
    const source = readFileSync("src/components/chat/MessageItem.tsx", "utf8");
    expect(source).not.toContain("#fafafa");
    expect(source).not.toContain("#e6f4ff");
  });

  // ===== 流式门控：chart 事件先于答案 token 到达，空气泡里不能先蹦出整张图 =====

  it("流式中且正文为空：图表槽显示骨架，不渲染图表", () => {
    renderMessage({ ...CHART_FIELDS, content: "", isStreaming: true });

    expect(screen.getByTestId("chart-skeleton")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-renderer")).toBeNull();
  });

  it("正文只有空白字符：仍视为未开始，显示骨架（只 trim 判据）", () => {
    renderMessage({ ...CHART_FIELDS, content: "  \n ", isStreaming: true });

    expect(screen.getByTestId("chart-skeleton")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-renderer")).toBeNull();
  });

  it("首个 token 到达：骨架让位于图表", () => {
    const { rerender } = renderMessage({ ...CHART_FIELDS, content: "", isStreaming: true });
    expect(screen.queryByTestId("chart-renderer")).toBeNull();

    rerenderMessage(rerender, { ...CHART_FIELDS, content: "2026-01 起", isStreaming: true });

    expect(screen.getByTestId("chart-renderer")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-skeleton")).toBeNull();
  });

  it("isStreaming=false 且正文为空：图表立即出现（非流式历史/降级形态）", () => {
    renderMessage({ ...CHART_FIELDS, content: "", isStreaming: false });

    expect(screen.getByTestId("chart-renderer")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-skeleton")).toBeNull();
  });

  it("isStreaming 未定义（持久化历史消息）：图表立即出现", () => {
    renderMessage({ ...CHART_FIELDS, content: "历史回答" });

    expect(screen.getByTestId("chart-renderer")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-skeleton")).toBeNull();
  });

  it("无 chartType 时骨架与图表都不渲染", () => {
    renderMessage({ content: "", isStreaming: true });

    expect(screen.queryByTestId("chart-skeleton")).toBeNull();
    expect(screen.queryByTestId("chart-renderer")).toBeNull();
  });

  it("骨架带 role=status 与加载文案（可访问性）", () => {
    renderMessage({ ...CHART_FIELDS, content: "", isStreaming: true });

    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText("图表生成中…")).toBeInTheDocument();
  });
});

describe("MessageItem 分节报告（2026-10-31）", () => {
  const steps = [
    {
      stepIndex: 0,
      description: "2024 销售额",
      subQuestion: "2024 年销售额是多少",
      aggregationOnly: false,
      status: "done" as const,
      chartType: "hbar" as const,
      chartOption: { series: [] },
    },
  ];

  it("按块顺序渲染正文与每步图", () => {
    // 断言文案刻意与 step.description 不同：MultiStepPlanCard 也会渲染步骤描述，
    // 用同一串文本会让 getByText 命中多个元素（那是测试写法问题，不是被测行为）。
    renderItem({
      content: "## 分析报告\n### 步骤 1：年度对比\n### 结论\n增长 20%。",
      reportSections: [
        { kind: "text", content: "## 分析报告\n### 步骤 1：年度对比" },
        { kind: "chart", stepIndex: 0, title: "2024 销售额" },
        { kind: "text", content: "### 结论\n增长 20%。" },
      ],
      steps,
    });

    expect(screen.getByText(/年度对比/)).toBeInTheDocument();
    const charts = screen.getAllByTestId("chart-renderer");
    expect(charts).toHaveLength(1);
    expect(charts[0].getAttribute("data-chart-type")).toBe("hbar");
    // 图在结论之前：分节顺序不能被卡片区（计划卡/查询计划）打乱
    const conclusion = screen.getByText("增长 20%。");
    expect(
      charts[0].compareDocumentPosition(conclusion) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it("chart 块取不到对应步骤时跳过（不崩、不画空图）", () => {
    renderItem({
      content: "### 步骤 1：A",
      reportSections: [
        { kind: "text", content: "### 步骤 1：A" },
        { kind: "chart", stepIndex: 9, title: "缺失" },
      ],
      steps,
    });

    expect(screen.queryByTestId("chart-renderer")).not.toBeInTheDocument();
  });

  it("reportSections 缺失时走旧 Markdown 渲染（反向守卫）", () => {
    renderItem({ content: "纯文本回答", steps });

    expect(screen.getByText("纯文本回答")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-renderer")).not.toBeInTheDocument();
  });
});
