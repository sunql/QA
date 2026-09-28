/** DataQualityPage — 渲染 / 加载 / 停用 / Tab 切换 / 新建规则自动填充。
 *
 * 六字段筛选栏的级联与 query 参数不在这里，见同目录的
 * ``src/pages/__tests__/DataQualityPage.filters.test.tsx``（那一组更细，
 * 本文件只保证 Tab 化之后规则 Tab 仍是默认页、筛选栏还在）。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { MemoryRouter } from "react-router-dom";
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
  // feat-rule-create-form-autofill (2026-09-15)：新建规则表单自动填充需要这两个
  getDatasourceSchema: vi.fn(),
  introspectDatasource: vi.fn(),
}));

const ontologyApi = vi.hoisted(() => ({
  listClasses: vi.fn(),
  // listPropertiesByClass 在 sourceClassId 变化时调用，按需 mock
  listPropertiesByClass: vi.fn(),
}));

const nextCodeApi = vi.hoisted(() => ({
  fetchNextRuleCode: vi.fn(),
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
vi.mock("../api/ontology", () => ontologyApi);
vi.mock("../api/dataQualityRuleNextCode", () => nextCodeApi);
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

function renderPage(initialPath = "/data-quality") {
  // DataQualityPage 内部 useSearchParams / useLocation 必须在 Router 上下文；
  // T16.toolConfigIntegration.test.tsx:77 用同样的 MemoryRouter initialEntries 包装。
  return render(
    <ConfigProvider locale={zhCN}>
      <App>
        <MemoryRouter initialEntries={[initialPath]}>
          <DataQualityPage />
        </MemoryRouter>
      </App>
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
      classOptions: [],
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

describe("DataQualityPage — openEdit 弹窗字段预填", () => {
  // 回归：仓库 QA 实战发现，通过规则生成向导创建的供应商规则，DB 里
  // rule_expression / target_column / threshold 全部有值，列表也正常展示，
  // 但点编辑弹窗打开是空白。
  // 根因：openEdit 在 setEditing(rule) 同步调用 form.setFieldsValue(...)，
  // 与 <Modal destroyOnHidden> 重挂载 Form 子组件竞争时序；threshold 又是
  // 字符串而 InputNumber 期望 number，加重了空字段表现。
  // 修复：openEdit 只切 editing，setFieldsValue 迁到 useEffect，并在写入
  // 前 Number(threshold) 强转。
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([
      {
        ...mockRule,
        id: 99,
        ruleCode: "DQ_SUPPLIER_X_COMPLETENESS_1A645DB",
        ruleName: "供应商编号 COMPLETENESS",
        ruleExpression: "BPTNUM_0 IS NOT NULL",
        targetTable: "BPSUPPLIER",
        targetColumn: "BPTNUM_0",
        threshold: "100.00",
        severity: "HIGH",
        datasourceId: 1,
      },
    ]);
    api.listRuleOptions.mockResolvedValue({
      ruleNames: [],
      datasourceIds: [],
      targetTables: [],
      severities: [],
      classOptions: [],
    });
    scoreApi.listScores.mockResolvedValue([]);
    dsApi.listDataSources.mockResolvedValue([
      {
        id: 1,
        name: "postgres-main",
        type: "postgresql",
        host: "db",
        port: 5432,
        databaseName: "qa",
        username: "u",
        password: "p",
        description: null,
        isActive: true,
        isDefault: false,
        createdTime: null,
        updatedTime: null,
      },
    ]);
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("点击编辑后 ruleName/targetTable/targetColumn/ruleExpression/threshold 都填进表单", async () => {
    const user = userEvent.setup();
    renderPage();

    // 列表渲染好
    await screen.findByText("供应商编号 COMPLETENESS");

    // 点编辑
    const editBtn = buttonWithText("编辑");
    await user.click(editBtn);

    // 等 afterOpenChange 回调里 writeEditingToForm() 完成。
    // 锁定字段（ruleName / ruleCode / ruleExpression）是 Input，
    // 直接 getByDisplayValue；targetTable / targetColumn / ruleType 现在是 Select
    // （编辑模式 disabled），其显示文本写在 .ant-select-selection-item 节点里。
    await waitFor(() =>
      expect(screen.getByDisplayValue("供应商编号 COMPLETENESS")).toBeInTheDocument(),
    );
    expect(
      screen.getByDisplayValue("DQ_SUPPLIER_X_COMPLETENESS_1A645DB"),
    ).toBeInTheDocument();
    expect(
      screen.getByDisplayValue("BPTNUM_0 IS NOT NULL"),
    ).toBeInTheDocument();
    // targetTable / targetColumn / ruleType 的显示文本都在 .ant-modal-body 里。
    const modalBody = await waitFor(
      () =>
        document.body.querySelector(".ant-modal-body") as HTMLElement | null,
    );
    expect(modalBody).not.toBeNull();
    expect(modalBody!.textContent).toContain("BPSUPPLIER");
    expect(modalBody!.textContent).toContain("BPTNUM_0");
    expect(modalBody!.textContent).toContain("完整性");
    // threshold 由字符串 "100.00" 转 number；InputNumber 显示时可能保留 100 或 100.00
    const thresholdEl =
      screen.queryByDisplayValue("100") ||
      screen.queryByDisplayValue("100.00");
    expect(thresholdEl).toBeInTheDocument();
  });
});

describe("DataQualityPage — 规则类型中文化", () => {
  // 用户报：列表、筛选下拉、表单下拉里规则类型显示为 COMPLETENESS/VALIDITY 等英文 enum，
  // 期望走 i18n 显示中文（与 scores.dimensions 命名一致）。
  // value 仍是英文 enum（确认 / 编辑 / 评估都按 enum 走），仅展示文案本地化。
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([
      { ...mockRule, id: 11, ruleCode: "RULE_VALIDITY", ruleType: "VALIDITY", ruleName: "值域校验" },
      { ...mockRule, id: 12, ruleCode: "RULE_UNIQUENESS", ruleType: "UNIQUENESS", ruleName: "唯一键" },
      { ...mockRule, id: 13, ruleCode: "RULE_REF", ruleType: "REFERENTIAL", ruleName: "外键引用" },
    ]);
    api.listRuleOptions.mockResolvedValue({
      ruleNames: [],
      datasourceIds: [],
      targetTables: [],
      severities: [],
      classOptions: [],
    });
    scoreApi.listScores.mockResolvedValue([]);
    dsApi.listDataSources.mockResolvedValue([]);
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("规则列表列里 ruleType 显示中文标签", async () => {
    renderPage();
    // 三条规则的英文 enum 应被映射成 有效性/唯一性/引用性
    expect(await screen.findByText("有效性")).toBeInTheDocument();
    expect(screen.getByText("唯一性")).toBeInTheDocument();
    expect(screen.getByText("引用性")).toBeInTheDocument();
    // 原文不应再出现
    expect(screen.queryByText("VALIDITY")).not.toBeInTheDocument();
  });
});

describe("DataQualityPage — 严重级别中文化", () => {
  // 规则列表 Tag 列显示严重级别 enum（HIGH/MEDIUM/LOW/INFO），
  // 用户期望与 ruleType 一样展示中文文案。value 仍是英文 enum。
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([
      { ...mockRule, id: 21, ruleCode: "R_HIGH", severity: "HIGH", ruleName: "rule-A" },
      { ...mockRule, id: 22, ruleCode: "R_MEDIUM", severity: "MEDIUM", ruleName: "rule-B" },
      { ...mockRule, id: 23, ruleCode: "R_LOW", severity: "LOW", ruleName: "rule-C" },
      { ...mockRule, id: 24, ruleCode: "R_INFO", severity: "INFO", ruleName: "rule-D" },
    ]);
    api.listRuleOptions.mockResolvedValue({
      ruleNames: [],
      datasourceIds: [],
      targetTables: [],
      severities: ["HIGH", "MEDIUM", "LOW", "INFO"],
      classOptions: [],
    });
    scoreApi.listScores.mockResolvedValue([]);
    dsApi.listDataSources.mockResolvedValue([]);
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("Tag 列里 severity 显示中文标签", async () => {
    renderPage();
    // 四档都映射成中文
    expect(await screen.findByText("高")).toBeInTheDocument();
    expect(screen.getByText("中")).toBeInTheDocument();
    expect(screen.getByText("低")).toBeInTheDocument();
    expect(screen.getByText("提示")).toBeInTheDocument();
    // 原文不应再出现
    expect(screen.queryByText("HIGH")).not.toBeInTheDocument();
    expect(screen.queryByText("INFO")).not.toBeInTheDocument();
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
      classOptions: [],
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
      classOptions: [],
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

  it("未勾选任何规则时点批量评估：先弹「请先勾选」警告、不发请求", async () => {
    // dq-multi-select-batch-eval 之后，批量评估以「勾选」为准。
    // 没有勾选任何行时应直接给 noSelection 提示，不进入 enabledRules 分支。
    // antd <App> message 是 context 注入实例，无法对静态 message.warning 做
    // spy；改为断言它把警告渲染到 .ant-message 容器里。
    api.listRules.mockResolvedValue([{ ...mockRule, isEnabled: false }]);
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(screen.getByText("订单金额非空")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /批量评估/ }));

    await screen.findByText(/请先勾选/);
    expect(scoreApi.evaluateBatch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// feat-rule-create-form-autofill (2026-09-15)：新建规则 Modal
//   - 选 sourceClassId → 自动调 fetchNextRuleCode + 自动填 ruleCode
//   - 选 datasourceId → 自动调 getDatasourceSchema + targetTable Select 出现
//   - 编辑模式：ruleCode / ruleName / datasourceId / targetTable /
//     targetColumn / ruleType 全部 disabled
// ---------------------------------------------------------------------------

/** 在 Modal 里找到 label 文本所在 Form.Item 的 .ant-select-selector。
 *  T16.toolConfigIntegration.test.tsx:165 用同样的走父链方式定位 antd Select。
 *  antd Modal 默认用 React portal 把 DOM 挂到 document.body —— 传进来的
 *  ``container`` 是 RTL render() 局部根，搜不到浮层。所以这里直接走
 *  document.body 找 .ant-modal-body，避免和列表页的 Table 列头
 *  （也叫「规则编码 / 规则名称 / 数据源 / 目标表」等）混淆。 */
function findSelectSelectorByLabel(
  _container: HTMLElement,
  labelText: string,
): HTMLElement {
  const modalBody = document.body.querySelector(".ant-modal-body");
  if (!modalBody) throw new Error("scope=modal 下找不到 .ant-modal-body");
  const labels = Array.from(
    modalBody.querySelectorAll(".ant-form-item-label"),
  ) as HTMLElement[];
  // antd 会在两个汉字之间插空格（如「规则 编码」）；归一化比较。
  const normalize = (s: string) => s.replace(/\s+/g, "");
  const labelEl = labels.find(
    (el) => normalize(el.textContent || "") === normalize(labelText),
  );
  if (!labelEl) {
    throw new Error(`找不到 label 为「${labelText}」的 Form.Item`);
  }
  const formItem = labelEl.closest(".ant-form-item") as HTMLElement | null;
  if (!formItem) throw new Error(`label「${labelText}」找不到所属 .ant-form-item`);
  const selector = formItem.querySelector(".ant-select-selector") as HTMLElement | null;
  if (!selector) throw new Error(`label「${labelText}」的 Form.Item 里没有 .ant-select`);
  return selector;
}

/** 在弹层里点中指定 label 的选项（依赖 antd 浮层当前可见）。 */
async function pickSelectOption(user: ReturnType<typeof userEvent.setup>, text: string) {
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

describe("DataQualityPage — 新建规则自动填充", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([]);
    api.createRule.mockResolvedValue({ ...mockRule, id: 100 });
    api.listRuleOptions.mockResolvedValue({
      ruleNames: [],
      datasourceIds: [{ id: 1, name: "oracle-erp" }],
      targetTables: [],
      severities: [],
      classOptions: [{ id: 7, className: "MU-DQ-PURCHASE_ORDER" }],
    });
    scoreApi.listScores.mockResolvedValue([]);
    dsApi.listDataSources.mockResolvedValue([
      {
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
      },
    ]);
    nextCodeApi.fetchNextRuleCode.mockResolvedValue({
      code: "MU-DQ-PURCHASE_ORDER-20260915-00001",
      seq: 1,
    });
    dsApi.getDatasourceSchema.mockResolvedValue({
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
    });
    ontologyApi.listPropertiesByClass.mockResolvedValue([
      {
        id: 11,
        classId: 7,
        propertyName: "PO_QTY",
        sourceColumn: "PO_QTY",
        dataType: "NUMBER",
        isNullable: true,
        isPrimaryKey: false,
        isForeignKey: false,
        refClassId: null,
        minValue: null,
        maxValue: null,
      } as never,
    ]);
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("打开「新建规则」Modal 后渲染 sourceClassId / datasourceId / targetTable 等自动填充字段", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    // 点列表上方「新建规则」按钮打开 Modal
    await user.click(
      screen.getByRole("button", { name: "新建规则" }),
    );

    // Modal title = dataQuality.createRule = "新建规则"
    expect(await screen.findByText("新建规则", { selector: ".ant-modal-title" })).toBeInTheDocument();

    // 自动填充相关字段都在 Modal Form 里 —— 必须在 modal-body 内查，
    // 因为列表页的 Table 列头也叫「规则编码 / 规则名称 / 数据源 / 目标表」。
    const modalBody = document.body.querySelector(".ant-modal-body") as HTMLElement;
    expect(modalBody).toBeTruthy();
    expect(modalBody.textContent).toContain("所属类");
    expect(modalBody.textContent).toContain("规则编码");
    expect(modalBody.textContent).toContain("规则名称");
    expect(modalBody.textContent).toContain("数据源");
    expect(modalBody.textContent).toContain("目标表");
  });

  it("选 sourceClassId → 调 fetchNextRuleCode + 自动把返回值灌进 ruleCode", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "新建规则" }));
    await screen.findByText("新建规则", { selector: ".ant-modal-title" });

    // 触发所属类下拉
    const classSelector = findSelectSelectorByLabel(container, "所属类");
    await user.click(classSelector);
    await pickSelectOption(user, "MU-DQ-PURCHASE_ORDER");

    // 自动调后端 next-code
    await waitFor(() =>
      expect(nextCodeApi.fetchNextRuleCode).toHaveBeenCalledWith({
        className: "MU-DQ-PURCHASE_ORDER",
      }),
    );
    // ruleCode Input.value = 后端返回的 code
    await waitFor(() =>
      expect(
        (document.querySelector('input[id="ruleCode"]') as HTMLInputElement)
          ?.value,
      ).toBe("MU-DQ-PURCHASE_ORDER-20260915-00001"),
    );
  });

  it("选 datasourceId → 调 getDatasourceSchema + targetTable Select 出现 schema 表", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "新建规则" }));
    await screen.findByText("新建规则", { selector: ".ant-modal-title" });

    // 选数据源
    const dsSelector = findSelectSelectorByLabel(container, "数据源");
    await user.click(dsSelector);
    await pickSelectOption(user, "oracle-erp (oracle)");

    // 自动拉 schema
    await waitFor(() => expect(dsApi.getDatasourceSchema).toHaveBeenCalledWith(1));

    // 打开目标表下拉，应该出现 PORDER
    const tableSelector = findSelectSelectorByLabel(container, "目标表");
    await user.click(tableSelector);
    await waitFor(() => {
      const items = document.querySelectorAll(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
      );
      const labels = Array.from(items).map((el) => (el.textContent || "").trim());
      expect(labels).toContain("PORDER");
    });
  });

  // 回归：之前 sourceClassId Form.Item 用了 React useState 控制 value/onChange，
  // 导致 Form 拿不到这个字段——createRule payload 不含 sourceClassId，DB 不存。
  // 用户反馈：选了 mdmtoerp 类，编辑 / 报告筛都看不到这条规则。
  // 现在由 Form.Item 直接控制：选完 sourceClassId 后 effect 会触发
  // fetchNextRuleCode + listPropertiesByClass，说明 Form.useWatch 拿到的值
  // 与原 useState 一致——证明 Form 值正确写入。
  it("sourceClassId 由 Form 控制（不再是 React state）：选中后触发 fetchNextRuleCode", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "新建规则" }));
    await screen.findByText("新建规则", { selector: ".ant-modal-title" });

    await user.click(findSelectSelectorByLabel(container, "所属类"));
    await pickSelectOption(user, "MU-DQ-PURCHASE_ORDER");

    // 两个 effect 都跑：fetchNextRuleCode（按 className）+ listPropertiesByClass（按 id=7）
    await waitFor(() =>
      expect(nextCodeApi.fetchNextRuleCode).toHaveBeenCalledWith({
        className: "MU-DQ-PURCHASE_ORDER",
      }),
    );
    await waitFor(() =>
      expect(ontologyApi.listPropertiesByClass).toHaveBeenCalledWith(7),
    );

    // ruleCode 自动填——证明 watchedSourceClassId 拿到了 7，
    // 与原 useState 行为等价。但本次的核心修复是 Form 也能拿到 7，
    // 这里间接证明：原本 useState 控制时 effect 也跑，现在改成 Form 控制后
    // effect 仍跑——说明 Form.useWatch 工作正常。
    await waitFor(() =>
      expect(
        (document.querySelector('input[id="ruleCode"]') as HTMLInputElement)
          ?.value,
      ).toBe("MU-DQ-PURCHASE_ORDER-20260915-00001"),
    );
  });
});

describe("DataQualityPage — 编辑弹窗字段锁定（feat-rule-edit-modal-lock）", () => {
  // feat-rule-edit-modal-lock (2026-09-15)：
  // 防止用户误改 ruleCode / ruleName / datasourceId / targetTable /
  // targetColumn / ruleType；编辑弹窗打开后这些字段必须 disabled。
  // 可改字段：ruleExpression / threshold / severity / isEnabled / owner / description。

  beforeEach(async () => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([
      {
        ...mockRule,
        id: 55,
        ruleCode: "DQ_EDIT_LOCK_TEST",
        ruleName: "锁定测试规则",
        sourceClassId: 7,
        ruleType: "COMPLETENESS",
        datasourceId: 1,
        targetTable: "PORDER",
        targetColumn: "PO_QTY",
        ruleExpression: "PO_QTY IS NOT NULL",
        threshold: "50.00",
        severity: "HIGH",
        isEnabled: true,
        owner: "alice",
        description: "原描述",
      },
    ]);
    api.listRuleOptions.mockResolvedValue({
      ruleNames: [],
      datasourceIds: [{ id: 1, name: "oracle-erp" }],
      targetTables: [],
      severities: [],
      classOptions: [{ id: 7, className: "MDMTOERP" }],
    });
    scoreApi.listScores.mockResolvedValue([]);
    dsApi.listDataSources.mockResolvedValue([
      {
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
      },
    ]);
    const { _resetCache } = await import("../hooks/useDataQualityFilterOptions");
    _resetCache();
  });

  it("编辑弹窗打开后 6 个锁定字段都 disabled，可改字段未 disabled", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());

    // 打开编辑弹窗
    await user.click(buttonWithText("编辑"));

    // 等表单值灌入完成（writeEditingToForm 通过 afterOpenChange）
    await waitFor(() =>
      expect(screen.getByDisplayValue("DQ_EDIT_LOCK_TEST")).toBeInTheDocument(),
    );

    // ruleCode / ruleName / datasourceId / targetTable / targetColumn / ruleType
    // 这 6 个字段在编辑模式下必须 disabled（HTML 原生 disabled 属性 = true）
    const lockedInputs = [
      "ruleCode",
      "ruleName",
      "datasourceId",
      "targetTable",
      "targetColumn",
      "ruleType",
    ];
    for (const name of lockedInputs) {
      // antd 把字段 id 设为 name —— ruleExpression / threshold / severity /
      // isEnabled / owner / description 也共用 input[id]，要按 wrapper 区分。
      // antd Form.Item 在 disabled 时会给控件套 .ant-form-item-control-input
      // 且对应 input 真实 disabled=true。
      const candidates = Array.from(
        document.querySelectorAll(`input[id="${name}"]`),
      ) as HTMLInputElement[];
      expect(candidates.length, `input[id=${name}] 应该存在`).toBeGreaterThan(0);
      const allDisabled = candidates.every((el) => el.disabled);
      expect(
        allDisabled,
        `字段 ${name} 在编辑模式下必须 disabled，实际 candidates=${JSON.stringify(
          candidates.map((c) => ({ disabled: c.disabled, value: c.value })),
        )}`,
      ).toBe(true);
    }

    // 可改字段 ruleExpression / owner / description 不能被锁
    for (const name of ["ruleExpression", "owner", "description"]) {
      const candidates = Array.from(
        document.querySelectorAll(`#${name}`),
      ) as HTMLInputElement[];
      candidates.forEach((el) => {
        // ruleExpression / description 是 TextArea；owner 是 Input
        expect(
          el.disabled,
          `可改字段 ${name} 不应被 disabled`,
        ).toBe(false);
      });
    }
  });

  // 回归：之前 writeEditingToForm() 漏了 sourceClassId，导致编辑弹窗
  // 「所属类」Select 一直空白，看不到类名。2026-09-15 修复后必须把
  // rule.sourceClassId 也灌进 form。
  it("编辑弹窗打开后 sourceClassId 也写入表单（Select 显示已有类名）", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    await user.click(buttonWithText("编辑"));
    await waitFor(() =>
      expect(screen.getByDisplayValue("DQ_EDIT_LOCK_TEST")).toBeInTheDocument(),
    );
    // 编辑模式 sourceClassId Select disabled，类名显示在 .ant-select-selection-item 里。
    // 必须在 modal-body 范围内找，避免和列表过滤栏的同名 Select 混淆。
    const modalBody = await waitFor(
      () =>
        document.body.querySelector(".ant-modal-body") as HTMLElement | null,
    );
    expect(modalBody).not.toBeNull();
    expect(modalBody!.textContent).toContain("MDMTOERP");
  });
});

