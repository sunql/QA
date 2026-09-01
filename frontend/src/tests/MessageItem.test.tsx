import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import MessageItem from "../components/chat/MessageItem";
import type { ChatMessage } from "../types/chat";

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
