/** DataQualityPage 5 字段筛选 + 级联 + 重置（feat-dq-rule-list-filters）。 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import DataQualityPage from "../DataQualityPage";
import type { DataQualityRule } from "../../types/dataQuality";

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

// 评分 Tab 的 API 也一并 mock：本组用例不碰它，但页面 import 了该模块，
// 不 mock 就是「测试里躺着一条能发真实 HTTP 的路径」。
const scoreApi = vi.hoisted(() => ({
  evaluateRule: vi.fn(),
  evaluateBatch: vi.fn(),
  computeScore: vi.fn(),
  listScores: vi.fn(),
}));

vi.mock("../../api/dataQuality", () => api);
vi.mock("../../api/datasource", () => dsApi);
vi.mock("../../api/dataQualityScore", () => scoreApi);

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

const MOCK_OPTIONS = {
  ruleNames: ["订单金额非空", "供应商去重"],
  datasourceIds: [
    { id: 1, name: "主库" },
    { id: 2, name: "备库" },
  ],
  targetTables: ["T_ORDER", "T_SUPPLIER", "PORDER"],
  severities: ["HIGH", "MEDIUM", "LOW", "INFO"],
};

function renderPage() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <DataQualityPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
}

// antd Select 把 data-testid 透传到 .ant-select 根 div 上
function getSelect(testId: string): HTMLElement {
  const el = document.querySelector(`[data-testid="${testId}"]`);
  if (!el) throw new Error(`Select with data-testid="${testId}" not found`);
  return el as HTMLElement;
}

describe("DataQualityPage — 5 字段筛选（feat-dq-rule-list-filters）", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([mockRule]);
    api.listRuleOptions.mockResolvedValue(MOCK_OPTIONS);
    dsApi.listDataSources.mockResolvedValue(MOCK_OPTIONS.datasourceIds);
    scoreApi.listScores.mockResolvedValue([]);
    // 重置 useDataQualityFilterOptions 模块级缓存，避免跨测试污染
    const { _resetCache } = await import("../../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("挂载时拉取 /rules/options + 渲染 5 个筛选 Select", async () => {
    const { container } = renderPage();
    await waitFor(() => expect(api.listRuleOptions).toHaveBeenCalled());
    // 5 个筛选下拉
    expect(container.querySelector('[data-testid="filter-rule-name"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="filter-datasource"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="filter-target-table"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="filter-rule-type"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="filter-severity"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="filter-enabled"]')).toBeTruthy();
  });

  it("数据源未选时，目标表 Select disabled", async () => {
    renderPage();
    await waitFor(() => expect(api.listRuleOptions).toHaveBeenCalled());
    const target = getSelect("filter-target-table");
    // antd Select disabled 时，根 div 加 .ant-select-disabled + aria-disabled
    const cls = target.className;
    expect(
      cls.includes("ant-select-disabled") ||
        target.getAttribute("aria-disabled") === "true",
    ).toBe(true);
  });

  it("数据源选中后目标表解除 disabled", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRuleOptions).toHaveBeenCalled());

    // antd Select 在 jsdom 下通过 mousedown 触发下拉浮层
    const dsSelector = getSelect("filter-datasource").querySelector(
      ".ant-select-selector",
    ) as HTMLElement;
    fireEvent.mouseDown(dsSelector);
    await user.click(
      await screen.findByText("主库", {
        selector: ".ant-select-item-option-content",
      }),
    );

    await waitFor(() => {
      const target = getSelect("filter-target-table");
      expect(target.className.includes("ant-select-disabled")).toBe(false);
    });
  });

  it("切换数据源时清空目标表（级联）", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRuleOptions).toHaveBeenCalled());

    // 选 ds1（主库）→ 目标表启用
    const dsSel1 = getSelect("filter-datasource").querySelector(
      ".ant-select-selector",
    ) as HTMLElement;
    fireEvent.mouseDown(dsSel1);
    await user.click(
      await screen.findByText("主库", {
        selector: ".ant-select-item-option-content",
      }),
    );
    await waitFor(() => {
      const target = getSelect("filter-target-table");
      expect(target.className.includes("ant-select-disabled")).toBe(false);
      // mount + 选主库 = 2 次
      expect(api.listRules).toHaveBeenCalledTimes(2);
    });

    // 切换到 ds2（备库）→ 目标表仍启用（datasourceId 仍有值），值被清空
    const dsSel2 = getSelect("filter-datasource").querySelector(
      ".ant-select-selector",
    ) as HTMLElement;
    fireEvent.mouseDown(dsSel2);
    await user.click(
      await screen.findByText("备库", {
        selector: ".ant-select-item-option-content",
      }),
    );

    await waitFor(() => {
      const target = getSelect("filter-target-table");
      // 仍启用（datasourceId=2 仍有值）
      expect(target.className.includes("ant-select-disabled")).toBe(false);
      // 最后一次 listRules 调用：datasourceId=2，targetTable 未带（已被清空）
      const lastCall = api.listRules.mock.calls[api.listRules.mock.calls.length - 1][0];
      expect(lastCall.datasourceId).toBe(2);
      expect(lastCall.targetTable).toBeUndefined();
    });
  });

  it("改 filter 触发 listRules 且带正确 query 参数", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRuleOptions).toHaveBeenCalled());

    await waitFor(() => expect(api.listRules).toHaveBeenCalledTimes(1));
    const initialCall = api.listRules.mock.calls[0][0];
    expect(initialCall.ruleType).toBeUndefined();
    expect(initialCall.enabled).toBeUndefined();

    // 选规则类型 COMPLETENESS
    const rtSelector = getSelect("filter-rule-type").querySelector(
      ".ant-select-selector",
    ) as HTMLElement;
    fireEvent.mouseDown(rtSelector);
    await user.click(
      await screen.findByText("COMPLETENESS", {
        selector: ".ant-select-item-option-content",
      }),
    );

    await waitFor(() => expect(api.listRules).toHaveBeenCalledTimes(2));
    const lastCall = api.listRules.mock.calls[api.listRules.mock.calls.length - 1][0];
    expect(lastCall.ruleType).toBe("COMPLETENESS");
  });

  it("「重置」按钮清空所有 filter 并刷新列表", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRuleOptions).toHaveBeenCalled());

    const rtSelector = getSelect("filter-rule-type").querySelector(
      ".ant-select-selector",
    ) as HTMLElement;
    fireEvent.mouseDown(rtSelector);
    await user.click(
      await screen.findByText("COMPLETENESS", {
        selector: ".ant-select-item-option-content",
      }),
    );
    await waitFor(() => expect(api.listRules).toHaveBeenCalledTimes(2));

    const resetBtn = screen.getByRole("button", { name: "重 置" });
    await user.click(resetBtn);

    await waitFor(() => {
      const lastCall = api.listRules.mock.calls[api.listRules.mock.calls.length - 1][0];
      expect(lastCall.ruleType).toBeUndefined();
      expect(lastCall.enabled).toBeUndefined();
    });
  });
});