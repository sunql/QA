/** DataQualityPage — 批量评估多选 + 类名过滤 + 目标表多选（dq-multi-select-batch-eval）。
 *
 * 改动范围（与既有 DataQualityPage.filters.test.tsx 不重叠，专测新行为）：
 *  - Table 行首 checkbox 多选，selectedIds → handleBatchEvaluate 走这条路径。
 *  - 勾选全 enabled → 直接送；勾选含 disabled → 警告「已忽略」+ 仍送 enabled 子集。
 *  - 未勾选 → noSelection 提示，不发请求。
 *  - 类名过滤（sourceClassId）：选 Supplier 后 listRules 入参含 sourceClassId。
 *  - 目标表多选（targetTables）：选 2 个后 listRules 入参含 targetTables: [...]。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, ConfigProvider } from "antd";
import { MemoryRouter } from "react-router-dom";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../i18n";
import DataQualityPage from "../pages/DataQualityPage";
import type { DataQualityRule } from "../types/dataQuality";

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

const scoreApi = vi.hoisted(() => ({
  evaluateRule: vi.fn(),
  evaluateBatch: vi.fn(),
  computeScore: vi.fn(),
  listScores: vi.fn(),
}));

vi.mock("../api/dataQuality", () => api);
vi.mock("../api/datasource", () => dsApi);
vi.mock("../api/dataQualityScore", () => scoreApi);

const MOCK_OPTIONS = {
  ruleNames: ["订单金额非空"],
  datasourceIds: [
    { id: 1, name: "主库" },
    { id: 2, name: "备库" },
  ],
  targetTables: ["T_ORDER", "PORDER", "PO_HEADER"],
  severities: ["HIGH", "MEDIUM", "LOW", "INFO"],
  classOptions: [
    { id: 11, className: "Supplier" },
    { id: 12, className: "Customer" },
  ],
};

function makeRule(overrides: Partial<DataQualityRule> = {}): DataQualityRule {
  return {
    id: 1,
    ruleCode: "R1",
    ruleName: "规则一",
    ruleType: "COMPLETENESS",
    datasourceId: 1,
    targetTable: "T_ORDER",
    targetColumn: null,
    ruleExpression: null,
    threshold: "0",
    severity: "MEDIUM",
    isEnabled: true,
    version: "1",
    owner: null,
    description: null,
    createdTime: null,
    updatedTime: null,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        {/* DataQualityPage 用 useNavigate/useSearchParams（跳转/筛选持久化），
            测试需 Router 上下文 */}
        <MemoryRouter>
          <App>
            <DataQualityPage />
          </App>
        </MemoryRouter>
      </ConfigProvider>
    </I18nextProvider>,
  );
}

function getSelect(testId: string): HTMLElement {
  const el = document.querySelector(`[data-testid="${testId}"]`);
  if (!el) throw new Error(`Select with data-testid="${testId}" not found`);
  return el as HTMLElement;
}

function buttonWithText(re: RegExp): HTMLElement {
  return screen.getByRole("button", { name: re }) as HTMLElement;
}

async function pickFromSelect(user: ReturnType<typeof userEvent.setup>, testId: string, text: string | RegExp): Promise<void> {
  // antd multi-select 的 .ant-select-selector 自身带 pointer-events:none，
  // mouseDown 走选择器容器，userEvent.click 选项走 textContent 匹配。
  const root = getSelect(testId);
  const selector = root.querySelector(".ant-select-selector") as HTMLElement;
  fireEvent.mouseDown(selector);
  await user.click(
    await screen.findByText(text, { selector: ".ant-select-item-option-content" }),
  );
}

async function checkRowAt(user: ReturnType<typeof userEvent.setup>, rowIndex: number): Promise<void> {
  // antd Table 行 checkbox：第一列 checkbox（行内非表头）
  const rowCheckboxes = document.querySelectorAll(
    ".ant-table-tbody .ant-table-row .ant-checkbox-input",
  );
  const target = rowCheckboxes[rowIndex] as HTMLElement;
  if (!target) throw new Error(`row ${rowIndex} checkbox not found`);
  await user.click(target);
}

describe("DataQualityPage — 批量评估多选（dq-multi-select-batch-eval）", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRuleOptions.mockResolvedValue(MOCK_OPTIONS);
    dsApi.listDataSources.mockResolvedValue(MOCK_OPTIONS.datasourceIds);
    scoreApi.listScores.mockResolvedValue([]);
    scoreApi.evaluateBatch.mockResolvedValue({
      // EvaluateBatchResponse 契约含 results（feat-eval-batch-result）
      results: [],
      totalCount: 3,
      passedCount: 3,
      failedCount: 0,
      summaryTotal: 3,
      summaryPassed: 3,
    });
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("勾 1 条规则 + 点批量评估：evaluateBatch 收到那 1 个 id", async () => {
    api.listRules.mockResolvedValue([
      makeRule({ id: 101, ruleCode: "R_A", ruleName: "A" }),
      makeRule({ id: 102, ruleCode: "R_B", ruleName: "B" }),
    ]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    await screen.findByText("A");

    await checkRowAt(user, 0); // 勾 R_A

    await user.click(buttonWithText(/批量评估/));
    await waitFor(() => expect(scoreApi.evaluateBatch).toHaveBeenCalled());
    const lastCall = scoreApi.evaluateBatch.mock.calls[scoreApi.evaluateBatch.mock.calls.length - 1][0] as {
      ruleIds: number[];
    };
    expect(lastCall.ruleIds).toEqual([101]);
  });

  it("勾 3 条混合（2 enabled + 1 disabled）：弹「已忽略」+ 仍送 enabled 子集", async () => {
    api.listRules.mockResolvedValue([
      makeRule({ id: 1, isEnabled: true }),
      makeRule({ id: 2, isEnabled: false }),
      makeRule({ id: 3, isEnabled: true }),
    ]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    await checkRowAt(user, 0);
    await checkRowAt(user, 1);
    await checkRowAt(user, 2);

    await user.click(buttonWithText(/批量评估/));
    await waitFor(() => expect(scoreApi.evaluateBatch).toHaveBeenCalled());
    const lastCall = scoreApi.evaluateBatch.mock.calls[scoreApi.evaluateBatch.mock.calls.length - 1][0] as {
      ruleIds: number[];
    };
    expect(lastCall.ruleIds.sort()).toEqual([1, 3]); // 跳过了 id=2 的 disabled
    // antd <App> message 在 .ant-message 容器渲染文本
    await screen.findByText(/已忽略.*未启用规则/);
  });

  it("未勾选任何行点批量评估：弹 noSelection 警告，不发请求", async () => {
    api.listRules.mockResolvedValue([makeRule({ id: 1, isEnabled: true })]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    // 不勾任何行直接点
    await user.click(buttonWithText(/批量评估/));
    await screen.findByText(/请先勾选/);
    expect(scoreApi.evaluateBatch).not.toHaveBeenCalled();
  });

  it("勾选全 disabled：弹「没有已启用」且不发请求", async () => {
    api.listRules.mockResolvedValue([
      makeRule({ id: 1, isEnabled: false }),
      makeRule({ id: 2, isEnabled: false }),
    ]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    await checkRowAt(user, 0);
    await checkRowAt(user, 1);

    await user.click(buttonWithText(/批量评估/));
    await screen.findByText(/没有已启用/);
    expect(scoreApi.evaluateBatch).not.toHaveBeenCalled();
  });
});

describe("DataQualityPage — 类名过滤（sourceClassId）", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRuleOptions.mockResolvedValue(MOCK_OPTIONS);
    dsApi.listDataSources.mockResolvedValue(MOCK_OPTIONS.datasourceIds);
    scoreApi.listScores.mockResolvedValue([]);
    scoreApi.evaluateBatch.mockResolvedValue({
      totalCount: 0, passedCount: 0, failedCount: 0,
      summaryTotal: 0, summaryPassed: 0,
    });
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("选类名「Supplier」后 listRules 入参含 sourceClassId=11", async () => {
    api.listRules.mockResolvedValue([]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    await pickFromSelect(user, "filter-class-name", /Supplier/);

    await waitFor(() => {
      const calls = api.listRules.mock.calls;
      const last = calls[calls.length - 1][0] as Record<string, unknown>;
      expect(last.sourceClassId).toBe(11);
    });
  });
});

describe("DataQualityPage — 目标表多选（targetTables）", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRuleOptions.mockResolvedValue(MOCK_OPTIONS);
    dsApi.listDataSources.mockResolvedValue(MOCK_OPTIONS.datasourceIds);
    scoreApi.listScores.mockResolvedValue([]);
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("选 datasource 后再选 2 个 targetTable，listRules 入参含 targetTables: [...]", async () => {
    api.listRules.mockResolvedValue([]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    // 选数据源以启用目标表
    await pickFromSelect(user, "filter-datasource", /主库/);

    // mode="multiple"：打开下拉后不关，直接点两个 option（first click 后
    // antd 5 的 multi-select 保留下拉浮层，第二次 click 仍能命中）。
    const targetSelector = getSelect("filter-target-table").querySelector(
      ".ant-select-selector",
    ) as HTMLElement;
    fireEvent.mouseDown(targetSelector);
    await user.click(
      await screen.findByText("T_ORDER", { selector: ".ant-select-item-option-content" }),
    );
    // 第二次点同一浮层中的另一项
    await user.click(
      await screen.findByText("PORDER", { selector: ".ant-select-item-option-content" }),
    );

    await waitFor(() => {
      const calls = api.listRules.mock.calls;
      const last = calls[calls.length - 1][0] as Record<string, unknown>;
      expect(last.datasourceId).toBe(1);
      expect(last.targetTables).toEqual(["T_ORDER", "PORDER"]);
    });
  });
});
