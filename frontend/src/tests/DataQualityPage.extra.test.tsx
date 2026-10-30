/** DataQualityPage — 补测：把该页函数覆盖率从 33.82% 拉到 ≥80%。
 *
 * 覆盖范围（对照 vitest json 报告里那 45 个未覆盖函数）：
 *  - 规则列表筛选：ruleName / severity / enabled 三个 Select 的 onChange
 *  - 工具条按钮：刷新 / 批量新建（路由跳转）/ 分页大小切换
 *  - 单条评估弹窗：evaluateRule 调用、结果渲染、关闭 / 取消、失败分支
 *  - 批量评估结果弹窗：统计卡、明细表 6 个 render、导出 CSV、保存到历史、关闭 / 取消
 *  - 批量评估历史抽屉：打开、空态、查看回放、关闭
 *  - 新建 / 编辑规则：createRule / updateRule 的 payload、取消、手改后不被自动填充覆盖、
 *    目标表下拉的 filterOption 模糊搜索
 *  - 质量评分 Tab：列表渲染（scoreColor / 5 个 render）、计算评分 scope、各类筛选
 *
 * 反假绿约定（照仓内 OntologyPage / DataQualityPage 既有范式）：
 *  - 断言一律落在「行为」上：调了哪个 api、payload 长什么样、状态落到哪；
 *    不写「渲染没崩」「文本存在」这类恒真断言。
 *  - 每个「未调用 / 禁用」断言都配一条「已调用 / 未禁用」的反向用例。
 *  - 不读表单内部 state（setFieldsValue / getFieldsValue 会克隆数组 ⇒ 恒真假绿），
 *    只断言提交出去的 payload 与 DOM 上真实渲染出的值。
 *  - antd Form 提交走 waitFor；validateFields 的 rejection 不构造（只提交填全的表单）。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import DataQualityPage from "../pages/DataQualityPage";
import type { DataQualityRule } from "../types/dataQuality";
import type {
  DataQualityScore,
  EvaluateBatchResponse,
  EvaluationResult,
} from "../types/dataQualityScore";
import {
  appendBatchEvalHistory,
  clearBatchEvalHistory,
  countBatchEvalHistory,
} from "../utils/batchEvalHistory";

// ---------------------------------------------------------------------------
// Mocks —— 每个 api 模块都要列全导出名（vi.mock 是整体替换，漏一个就 undefined）
// ---------------------------------------------------------------------------

const api = vi.hoisted(() => ({
  listRules: vi.fn(),
  createRule: vi.fn(),
  updateRule: vi.fn(),
  disableRule: vi.fn(),
  listRuleOptions: vi.fn(),
}));

const dsApi = vi.hoisted(() => ({
  listDataSources: vi.fn(),
  getDatasourceSchema: vi.fn(),
  introspectDatasource: vi.fn(),
}));

const ontologyApi = vi.hoisted(() => ({
  listClasses: vi.fn(),
  listPropertiesByClass: vi.fn(),
}));

const nextCodeApi = vi.hoisted(() => ({
  fetchNextRuleCode: vi.fn(),
}));

const scoreApi = vi.hoisted(() => ({
  evaluateRule: vi.fn(),
  evaluateBatch: vi.fn(),
  computeScore: vi.fn(),
  listScores: vi.fn(),
}));

// 导出 CSV 走 utils/download；mock 掉才能断言「点了导出 → 调了 downloadCsv 且行字段对」
const downloadApi = vi.hoisted(() => ({
  downloadCsv: vi.fn(),
  toCsv: vi.fn(),
  downloadBlob: vi.fn(),
}));

vi.mock("../api/dataQuality", () => api);
vi.mock("../api/datasource", () => dsApi);
vi.mock("../api/ontology", () => ontologyApi);
vi.mock("../api/dataQualityRuleNextCode", () => nextCodeApi);
vi.mock("../api/dataQualityScore", () => scoreApi);
vi.mock("../utils/download", () => downloadApi);

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const MOCK_OPTIONS = {
  ruleNames: ["订单金额非空", "唯一性校验"],
  datasourceIds: [
    { id: 1, name: "oracle-erp" },
    { id: 2, name: "pg-main" },
  ],
  targetTables: ["T_ORDER", "PORDER"],
  severities: ["HIGH", "MEDIUM", "LOW", "INFO"],
  classOptions: [{ id: 7, className: "MU-DQ-PURCHASE_ORDER" }],
};

const MOCK_DATASOURCE = {
  id: 1,
  name: "oracle-erp",
  type: "oracle",
  host: "db",
  port: 1521,
  databaseName: "ORCL",
  username: "u",
  password: "p",
  description: null,
  isActive: true,
  isDefault: false,
  createdTime: null,
  updatedTime: null,
};

const MOCK_SCHEMA = {
  tables: [
    {
      tableName: "PORDER",
      owner: "ORDUSER",
      columns: [
        { columnName: "PO_QTY", dataType: "NUMBER", nullable: true },
        { columnName: "ORDER_NO", dataType: "VARCHAR2(32)", nullable: false },
      ],
      primaryKeys: ["ORDER_NO"],
      foreignKeys: [],
    },
  ],
  cachedAt: "2026-09-15T00:00:00Z",
};

const EVAL_RESULT: EvaluationResult = {
  ruleId: 1,
  ruleCode: "R1",
  ruleType: "COMPLETENESS",
  datasourceId: 1,
  totalCount: 1000,
  passedCount: 900,
  passRate: 90,
  status: "PASS",
  evaluatedAt: "2026-09-01T00:00:00Z",
  durationMs: 123,
  message: "全部通过",
};

const BATCH_RESPONSE: EvaluateBatchResponse = {
  results: [
    {
      ruleId: 1,
      ruleCode: "R_A",
      ruleType: "COMPLETENESS",
      datasourceId: 1,
      totalCount: 1000,
      passedCount: 1000,
      passRate: 100,
      status: "PASS",
      evaluatedAt: "2026-09-01T00:00:00Z",
      durationMs: 10,
      message: null,
    },
    {
      ruleId: 2,
      ruleCode: "R_B",
      ruleType: "VALIDITY",
      datasourceId: 1,
      totalCount: 2000,
      passedCount: 600,
      passRate: 60,
      status: "FAIL",
      evaluatedAt: "2026-09-01T00:00:00Z",
      durationMs: 20,
      message: "存在空值",
    },
    {
      ruleId: 3,
      ruleCode: "R_C",
      ruleType: "UNIQUENESS",
      datasourceId: 1,
      totalCount: 3000,
      passedCount: 0,
      passRate: 0,
      status: "ERROR",
      evaluatedAt: "2026-09-01T00:00:00Z",
      durationMs: 30,
      message: "SQL 执行失败",
    },
  ],
  summaryTotal: 3,
  summaryPassed: 1,
};

function makeRule(overrides: Partial<DataQualityRule> = {}): DataQualityRule {
  return {
    id: 1,
    ruleName: "规则一",
    ruleCode: "R1",
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
    sourceClassId: null,
    createdTime: "2026-09-01T00:00:00Z",
    updatedTime: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

function makeScore(overrides: Partial<DataQualityScore> = {}): DataQualityScore {
  return {
    id: 1,
    targetTable: "T_ORDER",
    scoreType: "TABLE",
    completenessScore: "95.50",
    validityScore: "80.00",
    uniquenessScore: null,
    consistencyScore: "70.00",
    timelinessScore: null,
    referentialScore: null,
    overallScore: "92.30",
    evaluatedAt: "2026-09-01T08:00:00Z",
    evaluationDurationMs: 1234,
    rulesCount: 5,
    createdTime: null,
    updatedTime: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

/** antd 会在两个汉字之间插空格（「停 用」），比较前统一去空白。 */
function normalize(text: string): string {
  return text.replace(/\s+/g, "");
}

function buttonWithText(text: string): HTMLElement {
  const expected = normalize(text);
  const btn = screen
    .getAllByRole("button")
    .find((b) => normalize(b.textContent || "") === expected);
  if (!btn) throw new Error(`找不到文案为「${text}」的按钮`);
  return btn;
}

/** 文案带动态后缀的按钮（如「批量评估（已选 3）」）按正则匹配。 */
function buttonMatching(re: RegExp): HTMLElement {
  const btn = screen
    .getAllByRole("button")
    .find((b) => re.test(normalize(b.textContent || "")));
  if (!btn) throw new Error(`找不到匹配 ${re} 的按钮`);
  return btn;
}

/** 等弹窗真正关闭：destroyOnHidden ⇒ 整块卸载；否则 wrap 变 display:none。 */
async function expectModalClosed(): Promise<void> {
  await waitFor(() => {
    const wrap = document.querySelector(".ant-modal-wrap") as HTMLElement | null;
    const modal = document.querySelector(".ant-modal") as HTMLElement | null;
    if (!wrap || !modal) return;
    expect(wrap.getAttribute("style") || "").toContain("display: none");
  });
}

/** 表格行内按钮（行操作列有 评估 / 编辑 / 停用 三个）。 */
function rowButton(ruleName: string, label: string): HTMLElement {
  const cell = screen.getByText(ruleName).closest("tr");
  if (!cell) throw new Error(`找不到规则「${ruleName}」所在行`);
  const btn = within(cell as HTMLElement)
    .getAllByRole("button")
    .find((b) => normalize(b.textContent || "") === label);
  if (!btn) throw new Error(`行「${ruleName}」里找不到「${label}」按钮`);
  return btn;
}

/** Modal 浮层根（antd Modal 用 portal 挂到 body，别和列表 Table 混淆）。 */
function modalEl(): HTMLElement {
  const el = document.querySelector(".ant-modal");
  if (!el) throw new Error("找不到 Modal");
  return el as HTMLElement;
}

function drawerEl(): HTMLElement {
  const el = document.querySelector(".ant-drawer");
  if (!el) throw new Error("找不到 Drawer");
  return el as HTMLElement;
}

/** Modal 里某个 Form.Item 的根节点（按 label 文案定位）。 */
function modalField(labelText: string): HTMLElement {
  const body = modalEl().querySelector(".ant-modal-body");
  if (!body) throw new Error("Modal 里找不到 .ant-modal-body");
  const labels = Array.from(
    body.querySelectorAll(".ant-form-item-label"),
  ) as HTMLElement[];
  const labelEl = labels.find(
    (el) => normalize(el.textContent || "") === normalize(labelText),
  );
  if (!labelEl) throw new Error(`找不到 label 为「${labelText}」的 Form.Item`);
  const formItem = labelEl.closest(".ant-form-item") as HTMLElement | null;
  if (!formItem) throw new Error(`label「${labelText}」找不到所属 .ant-form-item`);
  return formItem;
}

/** Modal 里某个 Form.Item 内的 Select 触发器。 */
function modalSelect(labelText: string): HTMLElement {
  const selector = modalField(labelText).querySelector(
    ".ant-select-selector",
  ) as HTMLElement | null;
  if (!selector) throw new Error(`label「${labelText}」的 Form.Item 里没有 .ant-select`);
  return selector;
}

/** 页面（非 Modal）里按 placeholder 文案定位 Select 触发器。 */
function selectByPlaceholder(text: string): HTMLElement {
  const holders = Array.from(
    document.querySelectorAll(".ant-select-selection-placeholder"),
  ) as HTMLElement[];
  const ph = holders.find((h) => normalize(h.textContent || "") === normalize(text));
  if (!ph) throw new Error(`找不到 placeholder 为「${text}」的 Select`);
  const root = ph.closest(".ant-select") as HTMLElement | null;
  if (!root) throw new Error(`placeholder「${text}」找不到所属 .ant-select`);
  const selector = root.querySelector(".ant-select-selector") as HTMLElement | null;
  if (!selector) throw new Error(`placeholder「${text}」的 Select 里没有 .ant-select-selector`);
  return selector;
}

/** 在当前可见的 antd 浮层里点中指定选项。 */
async function pickSelectOption(
  user: ReturnType<typeof userEvent.setup>,
  text: string,
): Promise<void> {
  await waitFor(() => {
    const items = document.querySelectorAll(
      ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
    );
    const labels = Array.from(items).map((el) => (el.textContent || "").trim());
    expect(labels).toContain(text);
  });
  const items = document.querySelectorAll(
    ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
  );
  const target = Array.from(items).find(
    (el) => (el.textContent || "").trim() === text,
  ) as HTMLElement | undefined;
  if (!target) throw new Error(`找不到选项「${text}」`);
  await user.click(target);
}

/** 勾选表格第 N 行的 checkbox。 */
async function checkRowAt(
  user: ReturnType<typeof userEvent.setup>,
  rowIndex: number,
): Promise<void> {
  const rowCheckboxes = document.querySelectorAll(
    ".ant-table-tbody .ant-table-row .ant-checkbox-input",
  );
  const target = rowCheckboxes[rowIndex] as HTMLElement | undefined;
  if (!target) throw new Error(`row ${rowIndex} checkbox not found`);
  await user.click(target);
}

function renderPage(initialPath = "/data-quality") {
  return render(
    <ConfigProvider locale={zhCN}>
      <App>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route path="/data-quality" element={<DataQualityPage />} />
            {/* 「批量新建」按钮的落点，用一个占位路由证明 navigate 真的发生了 */}
            <Route
              path="/data-quality/rules/batch-create"
              element={<div data-testid="batch-create-page">批量新建规则页</div>}
            />
          </Routes>
        </MemoryRouter>
      </App>
    </ConfigProvider>,
  );
}

async function openCreateModal(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await waitFor(() => expect(api.listRules).toHaveBeenCalled());
  await user.click(screen.getByRole("button", { name: "新建规则" }));
  await screen.findByText("新建规则", { selector: ".ant-modal-title" });
}

beforeEach(async () => {
  vi.clearAllMocks();
  window.localStorage.clear();
  clearBatchEvalHistory();

  api.listRules.mockResolvedValue([makeRule()]);
  api.createRule.mockResolvedValue(makeRule({ id: 999 }));
  api.updateRule.mockResolvedValue(makeRule({ id: 1 }));
  api.disableRule.mockResolvedValue(undefined);
  api.listRuleOptions.mockResolvedValue(MOCK_OPTIONS);

  dsApi.listDataSources.mockResolvedValue([MOCK_DATASOURCE]);
  dsApi.getDatasourceSchema.mockResolvedValue(MOCK_SCHEMA);
  dsApi.introspectDatasource.mockResolvedValue(MOCK_SCHEMA);

  ontologyApi.listClasses.mockResolvedValue([]);
  ontologyApi.listPropertiesByClass.mockResolvedValue([]);

  nextCodeApi.fetchNextRuleCode.mockResolvedValue({
    code: "MU-DQ-PURCHASE_ORDER-20260915-00001",
    seq: 1,
  });

  scoreApi.listScores.mockResolvedValue([]);
  scoreApi.evaluateRule.mockResolvedValue(EVAL_RESULT);
  scoreApi.evaluateBatch.mockResolvedValue(BATCH_RESPONSE);
  scoreApi.computeScore.mockResolvedValue({
    evaluatedRules: 3,
    savedScores: 3,
    durationMs: 120,
    scores: [],
  });

  downloadApi.downloadCsv.mockReturnValue(undefined);

  // 筛选选项是模块级缓存，跨用例共享，不清会把上一个用例的 options 带进来
  const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
  _resetCache();
});

// ===========================================================================
// 1. 规则列表：筛选 onChange + 工具条按钮 + 分页
// ===========================================================================

describe("DataQualityPage — 列表筛选与工具条", () => {
  it("按规则名称过滤：选中后 listRules 入参含 ruleName", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    const selector = document.querySelector(
      '[data-testid="filter-rule-name"] .ant-select-selector',
    ) as HTMLElement;
    fireEvent.mouseDown(selector);
    await pickSelectOption(user, "订单金额非空");

    await waitFor(() => {
      const calls = api.listRules.mock.calls;
      const last = calls[calls.length - 1][0] as Record<string, unknown>;
      expect(last.ruleName).toBe("订单金额非空");
    });
    // 反向：默认那次请求不带 ruleName（证明上面的入参确实来自筛选动作）
    const first = api.listRules.mock.calls[0][0] as Record<string, unknown>;
    expect(first.ruleName).toBeUndefined();
  });

  it("按严重程度过滤：选中「高」后 listRules 入参含 severity='HIGH'", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    const selector = document.querySelector(
      '[data-testid="filter-severity"] .ant-select-selector',
    ) as HTMLElement;
    fireEvent.mouseDown(selector);
    await pickSelectOption(user, "高");

    await waitFor(() => {
      const calls = api.listRules.mock.calls;
      const last = calls[calls.length - 1][0] as Record<string, unknown>;
      expect(last.severity).toBe("HIGH");
    });
    const first = api.listRules.mock.calls[0][0] as Record<string, unknown>;
    expect(first.severity).toBeUndefined();
  });

  it("按启用状态过滤：选中「未启用」后 listRules 入参含 enabled='disabled'", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    const selector = document.querySelector(
      '[data-testid="filter-enabled"] .ant-select-selector',
    ) as HTMLElement;
    fireEvent.mouseDown(selector);
    await pickSelectOption(user, "未启用");

    await waitFor(() => {
      const calls = api.listRules.mock.calls;
      const last = calls[calls.length - 1][0] as Record<string, unknown>;
      expect(last.enabled).toBe("disabled");
    });
    // 反向：默认是 all ⇒ 不传 enabled 键
    const first = api.listRules.mock.calls[0][0] as Record<string, unknown>;
    expect(first.enabled).toBeUndefined();
  });

  it("点「刷新」重新拉一次规则列表", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    expect(api.listRules).toHaveBeenCalledTimes(1);

    await user.click(buttonWithText("刷新"));

    await waitFor(() => expect(api.listRules).toHaveBeenCalledTimes(2));
  });

  it("点「批量新建」跳转到批量新建规则页", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    // 反向：跳转前页面里没有那个占位节点
    expect(screen.queryByTestId("batch-create-page")).not.toBeInTheDocument();

    await user.click(buttonWithText("批量新建"));

    expect(await screen.findByTestId("batch-create-page")).toBeInTheDocument();
  });

  it("分页大小切到 50：pageSize 落到 localStorage（qa.dq.rules.pageSize）", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    const sizeSelector = document.querySelector(
      ".ant-pagination-options .ant-select-selector",
    ) as HTMLElement | null;
    expect(sizeSelector).toBeTruthy();
    fireEvent.mouseDown(sizeSelector as HTMLElement);
    await pickSelectOption(user, "50 条/页");

    await waitFor(() =>
      expect(window.localStorage.getItem("qa.dq.rules.pageSize")).toBe("50"),
    );
  });
});

// ===========================================================================
// 2. 单条评估弹窗
// ===========================================================================

describe("DataQualityPage — 单条评估弹窗", () => {
  it("点「评估」调 evaluateRule(id) 并弹出评估结果", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("规则一");

    await user.click(rowButton("规则一", "评估"));

    await waitFor(() => expect(scoreApi.evaluateRule).toHaveBeenCalledWith(1));
    const modal = modalEl();
    await waitFor(() => expect(within(modal).getByText("R1")).toBeInTheDocument());
    expect(within(modal).getByText("90.00%")).toBeInTheDocument();
    expect(within(modal).getByText("通过")).toBeInTheDocument();
    expect(within(modal).getByText("1,000")).toBeInTheDocument();
    expect(within(modal).getByText("900")).toBeInTheDocument();
    expect(within(modal).getByText("123 ms")).toBeInTheDocument();
    expect(within(modal).getByText("全部通过")).toBeInTheDocument();
  });

  it("评估结果弹窗「关闭」按钮关掉弹窗", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("规则一");
    await user.click(rowButton("规则一", "评估"));
    await waitFor(() => expect(scoreApi.evaluateRule).toHaveBeenCalled());

    await user.click(buttonWithText("关闭"));

    await expectModalClosed();
  });

  it("评估结果弹窗右上角 X（onCancel）关掉弹窗", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("规则一");
    await user.click(rowButton("规则一", "评估"));
    await waitFor(() => expect(scoreApi.evaluateRule).toHaveBeenCalled());

    const closeBtn = document.querySelector(
      ".ant-modal .ant-modal-close",
    ) as HTMLElement | null;
    expect(closeBtn).toBeTruthy();
    await user.click(closeBtn as HTMLElement);

    await expectModalClosed();
  });

  it("evaluateRule 失败时提示错误且不弹结果窗", async () => {
    const user = userEvent.setup();
    scoreApi.evaluateRule.mockRejectedValue(new Error("评估服务不可用"));
    renderPage();
    await screen.findByText("规则一");

    await user.click(rowButton("规则一", "评估"));

    expect(await screen.findByText("评估服务不可用")).toBeInTheDocument();
    expect(document.querySelector(".ant-modal")).toBeNull();
  });
});

// ===========================================================================
// 3. 批量评估结果弹窗
// ===========================================================================

describe("DataQualityPage — 批量评估结果弹窗", () => {
  async function runBatchEvaluate(
    user: ReturnType<typeof userEvent.setup>,
  ): Promise<void> {
    api.listRules.mockResolvedValue([
      makeRule({ id: 1, ruleCode: "R_A", ruleName: "A" }),
      makeRule({ id: 2, ruleCode: "R_B", ruleName: "B" }),
      makeRule({ id: 3, ruleCode: "R_C", ruleName: "C" }),
    ]);
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    await screen.findByText("A");
    await checkRowAt(user, 0);
    await checkRowAt(user, 1);
    await checkRowAt(user, 2);
    await user.click(buttonMatching(/^批量评估/));
    await waitFor(() => expect(scoreApi.evaluateBatch).toHaveBeenCalled());
  }

  it("批量评估后明细表渲染每条结果（状态/通过率/记录数/耗时/失败原因）", async () => {
    const user = userEvent.setup();
    await runBatchEvaluate(user);

    const modal = modalEl();
    const rowA = within(modal).getByText("R_A").closest("tr") as HTMLElement;
    const rowB = within(modal).getByText("R_B").closest("tr") as HTMLElement;
    const rowC = within(modal).getByText("R_C").closest("tr") as HTMLElement;

    // 状态 Tag：PASS 绿 / FAIL 橙 / ERROR 红
    expect(within(rowA).getByText("PASS")).toHaveClass("ant-tag-green");
    expect(within(rowB).getByText("FAIL")).toHaveClass("ant-tag-orange");
    expect(within(rowC).getByText("ERROR")).toHaveClass("ant-tag-red");

    // 通过率
    expect(within(rowA).getByText("100.00%")).toBeInTheDocument();
    expect(within(rowB).getByText("60.00%")).toBeInTheDocument();
    // 总记录数 / 通过数（千分位）
    expect(within(rowB).getByText("2,000")).toBeInTheDocument();
    expect(within(rowB).getByText("600")).toBeInTheDocument();
    // 耗时
    expect(within(rowB).getByText("20 ms")).toBeInTheDocument();
    // 失败原因：null → "-"，有值 → 原文
    expect(within(rowA).getByText("-")).toBeInTheDocument();
    expect(within(rowB).getByText("存在空值")).toBeInTheDocument();
  });

  it("点「导出 CSV」调 downloadCsv 且行字段与后端结果对齐", async () => {
    const user = userEvent.setup();
    await runBatchEvaluate(user);

    await user.click(buttonWithText("导出 CSV"));

    expect(downloadApi.downloadCsv).toHaveBeenCalledTimes(1);
    const [rows, filename] = downloadApi.downloadCsv.mock.calls[0] as [
      Record<string, unknown>[],
      string,
    ];
    expect(filename).toMatch(/^evaluation-batch-.+\.csv$/);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toEqual({
      ruleCode: "R_A",
      ruleType: "COMPLETENESS",
      status: "PASS",
      totalCount: 1000,
      passedCount: 1000,
      passRate: "100.00",
      durationMs: 10,
      message: "",
    });
    expect(rows[1]).toEqual({
      ruleCode: "R_B",
      ruleType: "VALIDITY",
      status: "FAIL",
      totalCount: 2000,
      passedCount: 600,
      passRate: "60.00",
      durationMs: 20,
      message: "存在空值",
    });
  });

  it("点「保存到历史」写入 localStorage 并提示成功", async () => {
    const user = userEvent.setup();
    await runBatchEvaluate(user);
    expect(countBatchEvalHistory()).toBe(0);

    await user.click(buttonWithText("保存到历史"));

    await waitFor(() => expect(countBatchEvalHistory()).toBe(1));
    expect(await screen.findByText("已保存到历史")).toBeInTheDocument();
  });

  it("点「关闭」关掉批量评估结果弹窗", async () => {
    const user = userEvent.setup();
    await runBatchEvaluate(user);

    await user.click(buttonWithText("关闭"));

    await expectModalClosed();
  });

  it("批量评估结果弹窗右上角 X（onCancel）关掉弹窗", async () => {
    const user = userEvent.setup();
    await runBatchEvaluate(user);

    const closeBtn = document.querySelector(
      ".ant-modal .ant-modal-close",
    ) as HTMLElement | null;
    expect(closeBtn).toBeTruthy();
    await user.click(closeBtn as HTMLElement);

    await expectModalClosed();
  });

  it("evaluateBatch 失败时提示错误且不弹结果窗", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([
      makeRule({ id: 1, ruleCode: "R_A", ruleName: "A" }),
    ]);
    scoreApi.evaluateBatch.mockRejectedValue(new Error("批量评估超时"));
    renderPage();
    await screen.findByText("A");
    await checkRowAt(user, 0);
    await user.click(buttonMatching(/^批量评估/));

    expect(await screen.findByText("批量评估超时")).toBeInTheDocument();
    expect(document.querySelector(".ant-modal")).toBeNull();
  });
});

// ===========================================================================
// 4. 批量评估历史抽屉
// ===========================================================================

describe("DataQualityPage — 批量评估历史抽屉", () => {
  it("点「历史」打开抽屉并列出已保存的批次", async () => {
    const user = userEvent.setup();
    appendBatchEvalHistory(BATCH_RESPONSE);
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    await user.click(screen.getByTestId("batch-eval-history-btn"));

    expect(await screen.findByText("批量评估历史")).toBeInTheDocument();
    const drawer = drawerEl();
    expect(within(drawer).getByRole("button", { name: "查看" })).toBeInTheDocument();
    // 批次摘要：3 条里 1 条 PASS、1 条 FAIL、1 条 ERROR
    expect(within(drawer).getByText("1 PASS")).toBeInTheDocument();
    expect(within(drawer).getByText("1 FAIL")).toBeInTheDocument();
    expect(within(drawer).getByText("1 ERROR")).toBeInTheDocument();
  });

  it("历史为空时抽屉显示空态、没有「查看」按钮", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    await user.click(screen.getByTestId("batch-eval-history-btn"));

    expect(await screen.findByText("暂无历史记录")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看" })).not.toBeInTheDocument();
  });

  it("点历史里的「查看」回放当时的批量结果，且「保存到历史」变为禁用", async () => {
    const user = userEvent.setup();
    appendBatchEvalHistory(BATCH_RESPONSE);
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    await user.click(screen.getByTestId("batch-eval-history-btn"));
    await screen.findByText("批量评估历史");
    await user.click(screen.getByRole("button", { name: "查看" }));

    const modal = modalEl();
    await waitFor(() => expect(within(modal).getByText("R_A")).toBeInTheDocument());
    // 来自历史的回放是只读的：不能再存一次
    expect(within(modal).getByRole("button", { name: "保存到历史" })).toBeDisabled();
  });

  it("抽屉右上角关闭按钮（onClose）关掉抽屉", async () => {
    const user = userEvent.setup();
    appendBatchEvalHistory(BATCH_RESPONSE);
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    await user.click(screen.getByTestId("batch-eval-history-btn"));
    await screen.findByText("批量评估历史");
    const closeBtn = document.querySelector(
      ".ant-drawer .ant-drawer-close",
    ) as HTMLElement | null;
    expect(closeBtn).toBeTruthy();
    await user.click(closeBtn as HTMLElement);

    // 抽屉关闭后 root 上的 ant-drawer-open 类被摘掉
    await waitFor(() => {
      expect(document.querySelector(".ant-drawer.ant-drawer-open")).toBeNull();
    });
  });
});

// ===========================================================================
// 5. 新建 / 编辑规则表单
// ===========================================================================

describe("DataQualityPage — 新建规则表单提交", () => {
  it("填完必填项点保存：createRule 收到完整 payload", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([]);
    renderPage();
    await openCreateModal(user);

    // 先手填编码 / 名称（会锁住自动填充，后面断言 payload 用的是用户填的值）
    await user.type(
      document.querySelector("input#ruleCode") as HTMLInputElement,
      "RULE_NEW_1",
    );
    await user.type(
      document.querySelector("input#ruleName") as HTMLInputElement,
      "自定义规则名",
    );

    await user.click(modalSelect("数据源"));
    await pickSelectOption(user, "oracle-erp (oracle)");
    await waitFor(() => expect(dsApi.getDatasourceSchema).toHaveBeenCalledWith(1));

    await user.click(modalSelect("目标表"));
    await pickSelectOption(user, "PORDER");

    await user.click(modalSelect("规则类型"));
    await pickSelectOption(user, "完整性");

    await user.click(screen.getByRole("button", { name: /保\s?存/ }));

    await waitFor(() => expect(api.createRule).toHaveBeenCalledTimes(1));
    expect(api.createRule).toHaveBeenCalledWith(
      expect.objectContaining({
        ruleCode: "RULE_NEW_1",
        ruleName: "自定义规则名",
        datasourceId: 1,
        targetTable: "PORDER",
        ruleType: "COMPLETENESS",
      }),
    );
  });

  it("createRule 失败时提示错误", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([]);
    api.createRule.mockRejectedValue(new Error("规则编码已存在"));
    renderPage();
    await openCreateModal(user);

    await user.type(
      document.querySelector("input#ruleCode") as HTMLInputElement,
      "RULE_NEW_1",
    );
    await user.type(
      document.querySelector("input#ruleName") as HTMLInputElement,
      "自定义规则名",
    );
    await user.click(modalSelect("数据源"));
    await pickSelectOption(user, "oracle-erp (oracle)");
    await waitFor(() => expect(dsApi.getDatasourceSchema).toHaveBeenCalledWith(1));
    await user.click(modalSelect("目标表"));
    await pickSelectOption(user, "PORDER");
    await user.click(modalSelect("规则类型"));
    await pickSelectOption(user, "完整性");

    await user.click(screen.getByRole("button", { name: /保\s?存/ }));

    expect(await screen.findByText("规则编码已存在")).toBeInTheDocument();
    // 反向：同一弹窗点保存成功时是会调 createRule 的（见上一条用例）
    expect(api.createRule).toHaveBeenCalled();
  });

  it("点「取消」关掉新建弹窗且不提交", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([]);
    renderPage();
    await openCreateModal(user);

    await user.click(screen.getByRole("button", { name: /取\s?消/ }));

    await expectModalClosed();
    expect(api.createRule).not.toHaveBeenCalled();
  });

  it("手改 ruleCode 后：后端建议编码不再覆盖用户输入", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([]);
    renderPage();
    await openCreateModal(user);

    await user.type(
      document.querySelector("input#ruleCode") as HTMLInputElement,
      "MY_CODE",
    );

    await user.click(modalSelect("所属类"));
    await pickSelectOption(user, "MU-DQ-PURCHASE_ORDER");

    // 建议编码确实回来了（证明不是「后端没返回」造成的假绿）
    await waitFor(() =>
      expect(nextCodeApi.fetchNextRuleCode).toHaveBeenCalledWith({
        className: "MU-DQ-PURCHASE_ORDER",
      }),
    );
    expect(await screen.findByText(/已根据所选类自动建议编码/)).toBeInTheDocument();

    await waitFor(() =>
      expect(
        (document.querySelector("input#ruleCode") as HTMLInputElement).value,
      ).toBe("MY_CODE"),
    );
  });

  it("手改 ruleName 后：自动拼接的名称不再覆盖用户输入", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([]);
    renderPage();
    await openCreateModal(user);

    await user.type(
      document.querySelector("input#ruleName") as HTMLInputElement,
      "我的规则名",
    );

    await user.click(modalSelect("数据源"));
    await pickSelectOption(user, "oracle-erp (oracle)");
    await waitFor(() => expect(dsApi.getDatasourceSchema).toHaveBeenCalledWith(1));
    await user.click(modalSelect("目标表"));
    await pickSelectOption(user, "PORDER");
    await user.click(modalSelect("规则类型"));
    await pickSelectOption(user, "完整性");

    await waitFor(() =>
      expect(
        (document.querySelector("input#ruleName") as HTMLInputElement).value,
      ).toBe("我的规则名"),
    );
  });

  it("目标表下拉支持大小写不敏感的模糊搜索（filterOption）", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([]);
    renderPage();
    await openCreateModal(user);

    await user.click(modalSelect("数据源"));
    await pickSelectOption(user, "oracle-erp (oracle)");
    await waitFor(() => expect(dsApi.getDatasourceSchema).toHaveBeenCalledWith(1));

    await user.click(modalSelect("目标表"));
    const searchInput = modalField("目标表").querySelector(
      "input",
    ) as HTMLInputElement;
    await user.type(searchInput, "po");

    // 小写 po 仍能命中 PORDER（filterOption 做了 toLowerCase）
    await waitFor(() => {
      const items = document.querySelectorAll(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
      );
      const labels = Array.from(items).map((el) => (el.textContent || "").trim());
      expect(labels).toContain("PORDER");
    });

    // 反向：搜不到的关键字会把选项过滤掉（证明确实走了 filterOption）
    await user.clear(searchInput);
    await user.type(searchInput, "zzz");
    await waitFor(() => {
      const items = document.querySelectorAll(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
      );
      const labels = Array.from(items).map((el) => (el.textContent || "").trim());
      expect(labels).not.toContain("PORDER");
    });
  });
});

describe("DataQualityPage — 编辑规则表单提交", () => {
  it("点「保存」调 updateRule(id, payload) 且 payload 带上原规则字段", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([
      makeRule({
        id: 42,
        ruleName: "待编辑规则",
        ruleCode: "R_EDIT",
        severity: "LOW",
        owner: "alice",
        description: "原描述",
        ruleExpression: "AMT IS NOT NULL",
        threshold: "50.00",
      }),
    ]);
    renderPage();
    await screen.findByText("待编辑规则");

    await user.click(rowButton("待编辑规则", "编辑"));
    await waitFor(() =>
      expect(screen.getByDisplayValue("待编辑规则")).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /保\s?存/ }));

    await waitFor(() => expect(api.updateRule).toHaveBeenCalledTimes(1));
    const [id, payload] = api.updateRule.mock.calls[0] as [
      number,
      Record<string, unknown>,
    ];
    expect(id).toBe(42);
    expect(payload).toEqual(
      expect.objectContaining({
        ruleName: "待编辑规则",
        ruleCode: "R_EDIT",
        datasourceId: 1,
        targetTable: "T_ORDER",
        ruleType: "COMPLETENESS",
        ruleExpression: "AMT IS NOT NULL",
        threshold: 50,
        severity: "LOW",
        owner: "alice",
        description: "原描述",
      }),
    );
  });

  it("updateRule 失败时提示错误", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([
      makeRule({ id: 42, ruleName: "待编辑规则" }),
    ]);
    api.updateRule.mockRejectedValue(new Error("规则已被他人修改"));
    renderPage();
    await screen.findByText("待编辑规则");

    await user.click(rowButton("待编辑规则", "编辑"));
    await waitFor(() =>
      expect(screen.getByDisplayValue("待编辑规则")).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /保\s?存/ }));

    expect(await screen.findByText("规则已被他人修改")).toBeInTheDocument();
    expect(api.updateRule).toHaveBeenCalled();
  });
});

// ===========================================================================
// 6. 质量评分 Tab
// ===========================================================================

describe("DataQualityPage — 质量评分 Tab", () => {
  beforeEach(() => {
    scoreApi.listScores.mockResolvedValue([
      makeScore({ id: 1, targetTable: "T_ORDER", overallScore: "95.00" }),
      makeScore({ id: 2, targetTable: "PORDER", overallScore: "75.00" }),
      makeScore({
        id: 3,
        targetTable: "T_ALL",
        scoreType: "GLOBAL",
        overallScore: "50.00",
      }),
    ]);
  });

  it("评分列表渲染：评分类型 / 综合评分配色 / 维度缺省 / 评估时间 / 耗时", async () => {
    renderPage("/data-quality?tab=scores");
    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalled());

    // 评分类型 Select 的下拉选项也带 TABLE / GLOBAL 文案（浮层常驻 DOM），
    // 所以整段断言都限定在评分表格内，避免命中下拉选项。
    const scoreTable = document.querySelector(".ant-table") as HTMLElement;
    await waitFor(() =>
      expect(within(scoreTable).getByText("T_ORDER")).toBeInTheDocument(),
    );
    const rowOrder = within(scoreTable).getByText("T_ORDER").closest("tr") as HTMLElement;
    const rowPurchase = within(scoreTable).getByText("PORDER").closest("tr") as HTMLElement;
    const rowGlobal = within(scoreTable).getByText("T_ALL").closest("tr") as HTMLElement;

    // 评分类型（TABLE / GLOBAL）
    expect(within(rowOrder).getByText("TABLE")).toBeInTheDocument();
    expect(within(rowGlobal).getByText("GLOBAL")).toBeInTheDocument();
    // 综合评分：>=90 绿 / >=70 橙 / 其余红
    expect(within(rowOrder).getByText("95.0")).toHaveClass("ant-tag-green");
    expect(within(rowPurchase).getByText("75.0")).toHaveClass("ant-tag-orange");
    expect(within(rowGlobal).getByText("50.0")).toHaveClass("ant-tag-red");
    // 维度缺省 → "-"（uniqueness / timeliness / referential 三列为 null）
    expect(within(rowOrder).getAllByText("-").length).toBe(3);
    // 评估时间 / 耗时 / 规则数
    expect(
      within(rowOrder).getByText(new Date("2026-09-01T08:00:00Z").toLocaleString()),
    ).toBeInTheDocument();
    expect(within(rowOrder).getByText("1234 ms")).toBeInTheDocument();
    expect(within(rowOrder).getByText("5")).toBeInTheDocument();
  });

  it("点「计算评分」调 computeScore（全空 scope ⇒ 三个字段都 undefined）", async () => {
    const user = userEvent.setup();
    renderPage("/data-quality?tab=scores");
    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalled());

    await user.click(buttonWithText("计算评分"));

    await waitFor(() =>
      expect(scoreApi.computeScore).toHaveBeenCalledWith({
        datasourceId: undefined,
        targetTables: undefined,
        ruleTypes: undefined,
      }),
    );
    expect(await screen.findByText("已计算 3 条评分")).toBeInTheDocument();
  });

  it("设置 scope 后计算评分：payload 带 datasourceId / targetTables / ruleTypes", async () => {
    const user = userEvent.setup();
    renderPage("/data-quality?tab=scores");
    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalled());

    fireEvent.mouseDown(selectByPlaceholder("评分范围：数据源"));
    await pickSelectOption(user, "oracle-erp");

    fireEvent.mouseDown(selectByPlaceholder("评分范围：目标表"));
    await pickSelectOption(user, "T_ORDER");
    await user.keyboard("{Escape}");

    fireEvent.mouseDown(selectByPlaceholder("评分范围：规则类型"));
    await pickSelectOption(user, "完整性");
    await user.keyboard("{Escape}");

    await user.click(buttonWithText("计算评分"));

    await waitFor(() =>
      expect(scoreApi.computeScore).toHaveBeenCalledWith({
        datasourceId: 1,
        targetTables: ["T_ORDER"],
        ruleTypes: ["COMPLETENESS"],
      }),
    );
  });

  it("按表名筛选：listScores 入参带 table", async () => {
    const user = userEvent.setup();
    renderPage("/data-quality?tab=scores");
    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalled());

    await user.type(screen.getByPlaceholderText("按表名筛选"), "T_ORDER");

    await waitFor(() => {
      const calls = scoreApi.listScores.mock.calls;
      const last = calls[calls.length - 1][0] as Record<string, unknown>;
      expect(last.table).toBe("T_ORDER");
    });
    const first = scoreApi.listScores.mock.calls[0][0] as Record<string, unknown>;
    expect(first.table).toBeUndefined();
  });

  it("按评分类型筛选：listScores 入参带 scoreType", async () => {
    const user = userEvent.setup();
    renderPage("/data-quality?tab=scores");
    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalled());

    fireEvent.mouseDown(selectByPlaceholder("按评分类型筛选"));
    await pickSelectOption(user, "TABLE");

    await waitFor(() => {
      const calls = scoreApi.listScores.mock.calls;
      const last = calls[calls.length - 1][0] as Record<string, unknown>;
      expect(last.scoreType).toBe("TABLE");
    });
    const first = scoreApi.listScores.mock.calls[0][0] as Record<string, unknown>;
    expect(first.scoreType).toBeUndefined();
  });

  it("「仅最新」筛选：listScores 入参带 latest=true", async () => {
    const user = userEvent.setup();
    renderPage("/data-quality?tab=scores");
    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalled());

    fireEvent.mouseDown(selectByPlaceholder("仅最新"));
    await pickSelectOption(user, "仅最新");

    await waitFor(() => {
      const calls = scoreApi.listScores.mock.calls;
      const last = calls[calls.length - 1][0] as Record<string, unknown>;
      expect(last.latest).toBe(true);
    });
    const first = scoreApi.listScores.mock.calls[0][0] as Record<string, unknown>;
    expect(first.latest).toBe(false);
  });

  it("点「刷新」重新拉一次评分列表", async () => {
    const user = userEvent.setup();
    renderPage("/data-quality?tab=scores");
    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalled());
    expect(scoreApi.listScores).toHaveBeenCalledTimes(1);

    await user.click(buttonWithText("刷新"));

    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalledTimes(2));
  });

  it("computeScore 失败时提示错误", async () => {
    const user = userEvent.setup();
    scoreApi.computeScore.mockRejectedValue(new Error("评分计算失败"));
    renderPage("/data-quality?tab=scores");
    await waitFor(() => expect(scoreApi.listScores).toHaveBeenCalled());

    await user.click(buttonWithText("计算评分"));

    expect(await screen.findByText("评分计算失败")).toBeInTheDocument();
    expect(scoreApi.computeScore).toHaveBeenCalled();
  });
});
