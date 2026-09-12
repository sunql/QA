/** DataQualityPage — 渲染 / 加载 / 停用 / Tab 切换。
 *
 * 六字段筛选栏的级联与 query 参数不在这里，见同目录的
 * ``src/pages/__tests__/DataQualityPage.filters.test.tsx``（那一组更细，
 * 本文件只保证 Tab 化之后规则 Tab 仍是默认页、筛选栏还在）。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { message } from "antd";
import DataQualityPage from "../pages/DataQualityPage";
import type { DataQualityRule } from "../types/dataQuality";

// 每个具名导出都要列出来：vi.mock 工厂是整体替换，漏掉的导出会变成 undefined。
// listRuleOptions 被 useDataQualityFilterOptions 在挂载时就调用 —— 漏了它，
// 规则 Tab 一渲染就 TypeError。
const api = vi.hoisted(() => ({
  listRules: vi.fn(),
  createRule: vi.fn(),
  updateRule: vi.fn(),
  disableRule: vi.fn(),
  listRuleOptions: vi.fn(),
}));

const dsApi = vi.hoisted(() => ({
  listDataSources: vi.fn(),
}));

// 评分 Tab 的 API：不 mock 的话点「质量评分」会发真实 HTTP。
const scoreApi = vi.hoisted(() => ({
  evaluateRule: vi.fn(),
  evaluateBatch: vi.fn(),
  computeScore: vi.fn(),
  listScores: vi.fn(),
}));

vi.mock("../api/dataQuality", () => api);
vi.mock("../api/datasource", () => dsApi);
vi.mock("../api/dataQualityScore", () => scoreApi);

const mockRule: DataQualityRule = {
  id: 1,
  ruleCode: "RULE_001",
  ruleName: "订单金额非空",
  ruleType: "COMPLETENESS",
  datasourceId: 1,
  targetTable: "T_ORDER",
  targetColumn: "AMOUNT",
  ruleExpression: null,
  threshold: "0",
  severity: "HIGH",
  isEnabled: true,
  version: "1",
  owner: null,
  description: null,
  createdTime: "2026-09-01T00:00:00Z",
  updatedTime: "2026-09-01T00:00:00Z",
};

/** antd 会在恰好两个汉字之间插空格，可访问名是「停 用」；比较前统一去空白。 */
function buttonWithText(text: string): HTMLElement {
  const btn = screen
    .getAllByRole("button")
    .find((b) => (b.textContent || "").replace(/\s+/g, "") === text);
  if (!btn) throw new Error(`找不到文案为「${text}」的按钮`);
  return btn;
}

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <DataQualityPage />
    </ConfigProvider>,
  );
}

describe("DataQualityPage — 渲染 + 加载", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([mockRule]);
    api.listRuleOptions.mockResolvedValue({
      ruleNames: [],
      datasourceIds: [],
      targetTables: [],
      severities: [],
    });
    scoreApi.listScores.mockResolvedValue([]);
    dsApi.listDataSources.mockResolvedValue([]);
    // 模块级缓存跨测试共享，不清会把上一个用例的 options 带进来
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("挂载时拉取规则列表并展示", async () => {
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    expect(await screen.findByText("订单金额非空")).toBeInTheDocument();
  });

  it("listRules 失败时组件不崩", async () => {
    api.listRules.mockRejectedValue(new Error("网络错误"));
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("默认停在「规则」Tab：六字段筛选栏仍在，且未误触评分接口", async () => {
    const { container } = renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    for (const id of [
      "filter-rule-name",
      "filter-datasource",
      "filter-target-table",
      "filter-rule-type",
      "filter-severity",
      "filter-enabled",
    ]) {
      expect(container.querySelector(`[data-testid="${id}"]`)).toBeTruthy();
    }
    // 非激活面板惰性渲染：评分 Tab 没被挂载就不该发请求
    expect(scoreApi.listScores).not.toHaveBeenCalled();
  });
});

describe("DataQualityPage — 启用/停用切换", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([mockRule]);
    api.disableRule.mockResolvedValue({ ...mockRule, isEnabled: false });
    api.listRuleOptions.mockResolvedValue({
      ruleNames: [],
      datasourceIds: [],
      targetTables: [],
      severities: [],
    });
    scoreApi.listScores.mockResolvedValue([]);
    dsApi.listDataSources.mockResolvedValue([]);
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("点击停用按钮调用 disableRule", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(screen.getByText("订单金额非空")).toBeInTheDocument());
    // 曾经这里断言的是字面量 "common.disable"（zh-CN 只有 disabled，key 缺失被原样渲染）。
    // 现在按真实文案断言，缺失的 key 会立刻让这条用例红。
    await user.click(buttonWithText("停用"));

    await waitFor(() => expect(api.disableRule).toHaveBeenCalledWith(1));
  });

  it("disableRule 失败时组件不崩", async () => {
    api.disableRule.mockRejectedValue(new Error("权限不足"));
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(screen.getByText("订单金额非空")).toBeInTheDocument());
    await user.click(buttonWithText("停用"));

    await waitFor(() => expect(api.disableRule).toHaveBeenCalledWith(1));
  });
});

describe("DataQualityPage — 质量评分 Tab", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([mockRule]);
    api.listRuleOptions.mockResolvedValue({
      ruleNames: [],
      datasourceIds: [],
      targetTables: [],
      severities: [],
    });
    scoreApi.listScores.mockResolvedValue([]);
    dsApi.listDataSources.mockResolvedValue([]);
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("切到「质量评分」才拉评分列表，且带 limit=100", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    await user.click(screen.getByRole("tab", { name: /质量评分/ }));

    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalled());
    expect(scoreApi.listScores).toHaveBeenCalledWith({
      table: undefined,
      scoreType: undefined,
      latest: false,
      limit: 100,
    });
  });

  it("没有已启用规则时批量评估只提示、不发请求", async () => {
    const warnSpy = vi
      .spyOn(message, "warning")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.warning>);
    api.listRules.mockResolvedValue([{ ...mockRule, isEnabled: false }]);
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(screen.getByText("订单金额非空")).toBeInTheDocument());
    await user.click(buttonWithText("批量评估"));

    await waitFor(() =>
      expect(warnSpy).toHaveBeenCalledWith(
        expect.stringContaining("没有已启用的规则"),
      ),
    );
    expect(scoreApi.evaluateBatch).not.toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});
