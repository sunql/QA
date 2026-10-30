/** DataQualityRuleParamsPage — 规则参数结构化配置页（feat-dq-rule-params Task 11）
 * 2026-09-15：列表列头中文 + 新建弹窗（自动编码/自动命名/级联选择）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { i18n } from "../i18n";
import {
  DataQualityRuleParamsPage,
  buildAutoRuleName,
} from "../pages/DataQualityRuleParamsPage";

const api = vi.hoisted(() => ({
  listRules: vi.fn().mockResolvedValue([]),
  createRule: vi.fn().mockResolvedValue({}),
  getRule: vi.fn().mockResolvedValue(null),
  updateRule: vi.fn().mockResolvedValue({}),
  deleteRule: vi.fn().mockResolvedValue(undefined),
  fetchNextRuleCode: vi.fn().mockResolvedValue({
    code: "DQ-Rule-20260915-0000000001",
    seq: 1,
  }),
}));

const ontologyApi = vi.hoisted(() => ({
  listClasses: vi.fn().mockResolvedValue([]),
}));

const datasourceApi = vi.hoisted(() => ({
  listDataSources: vi.fn().mockResolvedValue([]),
  getDatasourceSchema: vi.fn().mockRejectedValue(new Error("no schema")),
  introspectDatasource: vi.fn().mockResolvedValue({ tables: [] }),
}));

// 行操作「停用 / 评估」走另外两个 API 模块（页面从 ../api/dataQuality 与
// ../api/dataQualityScore 引入），单独 mock 才能断言「被调用 / 未被调用」。
const dqApi = vi.hoisted(() => ({
  disableRule: vi.fn(),
}));
const scoreApi = vi.hoisted(() => ({
  evaluateRule: vi.fn(),
}));

vi.mock("../api/dataQualityRuleParams", () => api);
vi.mock("../api/ontology", () => ontologyApi);
vi.mock("../api/datasource", () => datasourceApi);
vi.mock("../api/dataQuality", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/dataQuality")>();
  return { ...actual, disableRule: dqApi.disableRule };
});
vi.mock("../api/dataQualityScore", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/dataQualityScore")>();
  return { ...actual, evaluateRule: scoreApi.evaluateRule };
});

describe("buildAutoRuleName", () => {
  it("拼接 数据源名称-类名-规则名（英文）", () => {
    expect(
      buildAutoRuleName({
        datasourceName: "THBI",
        className: "PO_LINE",
        ruleType: "VALIDITY",
      }),
    ).toBe("THBI-PO_LINE-Validity");
  });

  it("未知规则类型回退原值", () => {
    expect(
      buildAutoRuleName({
        datasourceName: "DS",
        className: "CLS",
        ruleType: "UNKNOWN_X",
      }),
    ).toBe("DS-CLS-UNKNOWN_X");
  });
});

describe("DataQualityRuleParamsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([]);
    api.fetchNextRuleCode.mockResolvedValue({
      code: "DQ-Rule-20260915-0000000001",
      seq: 1,
    });
    ontologyApi.listClasses.mockResolvedValue([]);
    datasourceApi.listDataSources.mockResolvedValue([]);
    i18n.changeLanguage("en-US");
  });

  it("renders title", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <DataQualityRuleParamsPage />
      </I18nextProvider>,
    );
    expect(screen.getByText(/Rule Config \(Structured\)/i)).toBeInTheDocument();
  });

  it("renders Chinese table headers in zh-CN", async () => {
    i18n.changeLanguage("zh-CN");
    api.listRules.mockResolvedValue([
      {
        id: 1,
        ruleCode: "DQ-Rule-20260915-0000000001",
        ruleName: "THBI-PO_LINE-Validity",
        ruleType: "VALIDITY",
        targetTable: "PO_LINE",
        targetColumn: "ORDER_QTY",
        threshold: "95.00",
        severity: "MEDIUM",
        datasourceId: 1,
        ruleExpression: "ORDER_QTY BETWEEN 0 AND 100",
        ruleParams: { kind: "range", min: 0, max: 100 },
        configMode: "structured",
        isEnabled: true,
        owner: "数据治理部",
      },
    ]);
    render(
      <I18nextProvider i18n={i18n}>
        <DataQualityRuleParamsPage />
      </I18nextProvider>,
    );
    // 规则 tab 对齐的全部列头
    expect(await screen.findByText("规则编码")).toBeInTheDocument();
    expect(screen.getByText("规则名称")).toBeInTheDocument();
    expect(screen.getByText("数据源")).toBeInTheDocument();
    expect(screen.getByText("目标表")).toBeInTheDocument();
    expect(screen.getByText("规则类型")).toBeInTheDocument();
    expect(screen.getByText("严重级别")).toBeInTheDocument();
    expect(screen.getByText("阈值（%）")).toBeInTheDocument();
    expect(screen.getByText("启用")).toBeInTheDocument();
    expect(screen.getByText("责任方")).toBeInTheDocument();
    expect(screen.getByText("操作")).toBeInTheDocument();
    // 本页特有列
    expect(screen.getByText("配置模式")).toBeInTheDocument();
    // 规则类型值映射中文 + 治理列渲染
    expect(screen.getByText("有效性")).toBeInTheDocument();
    expect(screen.getByText("中")).toBeInTheDocument();
    expect(screen.getByText("95.00")).toBeInTheDocument();
    expect(screen.getByText("是")).toBeInTheDocument();
    expect(screen.getByText("数据治理部")).toBeInTheDocument();
    // 操作列按钮（antd 两字中文按钮会插空格，name 用 \s* 容忍）
    expect(screen.getByRole("button", { name: /评\s*估/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /编\s*辑/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /停\s*用/ })).toBeEnabled();
  });

  it("create modal auto-fills code from next-code endpoint", async () => {
    i18n.changeLanguage("zh-CN");
    render(
      <I18nextProvider i18n={i18n}>
        <DataQualityRuleParamsPage />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByText("新建结构化规则"));
    // 弹窗打开后自动调用 next-code 并回填只读编码框
    await waitFor(() => {
      expect(api.fetchNextRuleCode).toHaveBeenCalled();
    });
    await waitFor(() => {
      const inputs = document.querySelectorAll<HTMLInputElement>("input");
      const codeInput = Array.from(inputs).find(
        (el) => el.value === "DQ-Rule-20260915-0000000001",
      );
      expect(codeInput).toBeDefined();
      expect(codeInput?.readOnly).toBe(true);
    });
    // 编码/名称标签为中文
    expect(screen.getByText("自动生成，格式：DQ-Rule-年月日-10位流水")).toBeInTheDocument();
    expect(screen.getByText("自动生成：数据源名称-类名-规则名（英文）")).toBeInTheDocument();
  });
});

// =============================================================================
// 行为覆盖：行操作（评估 / 停用 / 编辑）+ 新建弹窗（级联 / 结构化 / 自定义）
// 断言一律落在「提交出去的 payload」与「API 是否被调用」上，不用 setFieldsValue
// 回读表单值（rc-field-form 克隆数组 ⇒ 恒真假绿）。
// =============================================================================

const RULE_ROWS = [
  {
    id: 1,
    ruleCode: "DQ-Rule-20260915-0000000001",
    ruleName: "THBI-PO_LINE-Validity",
    ruleType: "VALIDITY",
    targetTable: "PO_LINE",
    targetColumn: "ORDER_QTY",
    threshold: "95.00",
    severity: "HIGH",
    datasourceId: 1,
    ruleExpression: null,
    ruleParams: { kind: "range", min: 0, max: 100 },
    configMode: "structured",
    isEnabled: true,
    owner: "数据治理部",
  },
  {
    id: 2,
    ruleCode: "DQ-Rule-20260915-0000000002",
    ruleName: "ODS-SUPPLIER-Custom",
    ruleType: "COMPLETENESS",
    targetTable: "ODS_SUPPLIER",
    targetColumn: null,
    threshold: "100.00",
    severity: "LOW",
    // 99 号数据源不存在 → 列表列回退 "#99"
    datasourceId: 99,
    ruleExpression: "SUPPLIER_NAME IS NOT NULL",
    ruleParams: null,
    configMode: "custom",
    isEnabled: false,
    owner: null,
  },
];

const DATASOURCES = [
  {
    id: 1,
    name: "THBI",
    type: "ORACLE",
    host: "h",
    port: 1521,
    databaseName: "d",
    username: "u",
    description: null,
    isActive: true,
    isDefault: true,
    oracleVersion: "19c",
    createdBy: null,
    createdTime: "2026-09-01T00:00:00Z",
    updatedTime: "2026-09-01T00:00:00Z",
  },
  {
    id: 2,
    name: "ODS",
    type: "POSTGRESQL",
    host: "h2",
    port: 5432,
    databaseName: "d2",
    username: "u2",
    description: null,
    isActive: true,
    isDefault: false,
    oracleVersion: null,
    createdBy: null,
    createdTime: "2026-09-01T00:00:00Z",
    updatedTime: "2026-09-01T00:00:00Z",
  },
];

const CLASSES = [
  {
    id: 10,
    className: "PO_LINE",
    classAlias: "采购订单行",
    description: null,
    sourceTable: "PO_LINE",
    parentClassId: null,
    objectType: null,
    objectOwner: null,
    createdBy: null,
    createdTime: null,
    updatedTime: null,
    version: 1,
    validFrom: null,
    validTo: null,
  },
  {
    id: 11,
    className: "SUPPLIER",
    classAlias: "供应商",
    description: null,
    sourceTable: "ODS_SUPPLIER",
    parentClassId: null,
    objectType: null,
    objectOwner: null,
    createdBy: null,
    createdTime: null,
    updatedTime: null,
    version: 1,
    validFrom: null,
    validTo: null,
  },
];

const SCHEMA = {
  tables: [
    {
      tableName: "PO_LINE",
      columns: [
        { columnName: "ORDER_QTY", dataType: "NUMBER" },
        { columnName: "STATUS", dataType: "STRING" },
      ],
    },
    {
      tableName: "ODS_SUPPLIER",
      columns: [
        { columnName: "SUPPLIER_ID", dataType: "NUMBER" },
        { columnName: "SUPPLIER_NAME", dataType: "STRING" },
      ],
    },
  ],
};

const EVAL_RESULT = {
  ruleId: 1,
  ruleCode: "DQ-Rule-20260915-0000000001",
  ruleType: "VALIDITY",
  datasourceId: 1,
  totalCount: 1000,
  passedCount: 950,
  passRate: 95,
  status: "PASS",
  evaluatedAt: "2026-09-15T00:00:00Z",
  durationMs: 12,
  message: "评估完成",
};

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={["/dq-rule-params"]}>
          <Routes>
            <Route path="/dq-rule-params" element={<DataQualityRuleParamsPage />} />
          </Routes>
        </MemoryRouter>
      </I18nextProvider>
    </ConfigProvider>,
  );
}

/** 打开作用域内的第一个 antd Select（项目既定模式：mouseDown .ant-select-selector）。 */
function openSelect(scope: HTMLElement) {
  const selector = scope.querySelector(".ant-select-selector");
  if (!selector) throw new Error("antd Select not found");
  fireEvent.mouseDown(selector);
}

/** 点击下拉选项（option 挂在 body portal 上，只能按文档范围找）。
 *  antd 下拉关闭后 DOM 仍保留（带 ant-select-dropdown-hidden），
 *  必须过滤掉隐藏下拉的同名选项，否则 getByText 会命中多个。 */
function pickOption(text: string | RegExp) {
  const all = screen.getAllByText(text, {
    selector: ".ant-select-item-option-content",
  });
  const visible = all.filter(
    (el) =>
      !el
        .closest(".ant-select-dropdown")
        ?.classList.contains("ant-select-dropdown-hidden"),
  );
  if (visible.length !== 1) {
    throw new Error(
      `expected exactly 1 visible option for ${String(text)}, got ${visible.length}`,
    );
  }
  fireEvent.click(visible[0]);
}

/** 按 Form.Item 的 label 文本定位表单项容器（无 name 的 Select 没有 id，靠 label 定位）。 */
function formItemByLabel(scope: HTMLElement, label: string): HTMLElement {
  const labelEl = within(scope).getByText(label);
  const item = labelEl.closest(".ant-form-item");
  if (!item) throw new Error(`Form.Item not found for label: ${label}`);
  return item as HTMLElement;
}

/** 取 Select 当前选中项文本（证明级联回填真的落到了 UI 上）。 */
function selectedText(scope: HTMLElement): string | null {
  return scope.querySelector(".ant-select-selection-item")?.textContent ?? null;
}

describe("DataQualityRuleParamsPage — 行操作与新建弹窗", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await i18n.changeLanguage("zh-CN");
    api.listRules.mockResolvedValue(RULE_ROWS);
    api.createRule.mockResolvedValue({});
    api.updateRule.mockResolvedValue({});
    api.fetchNextRuleCode.mockResolvedValue({
      code: "DQ-Rule-20260915-0000000001",
      seq: 1,
    });
    ontologyApi.listClasses.mockResolvedValue(CLASSES);
    datasourceApi.listDataSources.mockResolvedValue(DATASOURCES);
    datasourceApi.getDatasourceSchema.mockResolvedValue(SCHEMA);
    datasourceApi.introspectDatasource.mockResolvedValue(SCHEMA);
    dqApi.disableRule.mockResolvedValue(undefined);
    scoreApi.evaluateRule.mockResolvedValue(EVAL_RESULT);
  });

  // ---------- 列表列渲染 ----------

  it("数据源列渲染数据源名称；数据源已删除时回退 #id", async () => {
    renderPage();
    expect(await screen.findByText("THBI")).toBeInTheDocument();
    expect(screen.getByText("#99")).toBeInTheDocument();
  });

  it("规则参数列汇总结构化参数；自定义模式回落展示规则表达式", async () => {
    renderPage();
    // summarizeRuleParams(zh-CN)：ORDER_QTY ∈ [0, 100]
    expect(await screen.findByText("ORDER_QTY ∈ [0, 100]")).toBeInTheDocument();
    expect(screen.getByText("SUPPLIER_NAME IS NOT NULL")).toBeInTheDocument();
  });

  // ---------- 评估 ----------

  it("点击「评估」调用 evaluateRule 并展示评估结果弹窗", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    await user.click(screen.getAllByRole("button", { name: /评\s*估/ })[0]);

    await waitFor(() => {
      expect(scoreApi.evaluateRule).toHaveBeenCalledTimes(1);
      expect(scoreApi.evaluateRule).toHaveBeenCalledWith(1);
    });
    expect(await screen.findByText("95.00%")).toBeInTheDocument();
    expect(screen.getByText("1,000")).toBeInTheDocument();
    expect(screen.getByText("12 ms")).toBeInTheDocument();
    expect(screen.getByText("评估完成")).toBeInTheDocument();
  });

  it("评估结果弹窗：底部「关闭」按钮关闭弹窗", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    await user.click(screen.getAllByRole("button", { name: /评\s*估/ })[0]);
    await screen.findByText("95.00%");
    // antd 两字中文按钮会插空格（关 闭），name 用 \s* 容忍
    await user.click(screen.getByRole("button", { name: /关\s*闭/ }));
    // antd Modal 关闭时默认保留隐藏 DOM；可访问性树上的 dialog 消失才是行为判据。
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("评估结果弹窗：右上角关闭图标关闭弹窗", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    await user.click(screen.getAllByRole("button", { name: /评\s*估/ })[0]);
    await screen.findByText("95.00%");
    fireEvent.click(screen.getByRole("button", { name: /Close/ }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  // ---------- 停用 ----------

  it("点击「停用」调用 disableRule 并刷新列表", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");
    expect(api.listRules).toHaveBeenCalledTimes(1);

    await user.click(screen.getAllByRole("button", { name: /停\s*用/ })[0]);

    await waitFor(() => {
      expect(dqApi.disableRule).toHaveBeenCalledTimes(1);
      expect(dqApi.disableRule).toHaveBeenCalledWith(1);
    });
    // 停用成功后 refresh() 重新拉列表
    await waitFor(() => {
      expect(api.listRules).toHaveBeenCalledTimes(2);
    });
  });

  it("已停用规则的「停用」按钮禁用，点击不会调用 disableRule", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    const buttons = screen.getAllByRole("button", { name: /停\s*用/ });
    expect(buttons[1]).toBeDisabled();
    await user.click(buttons[1]);

    expect(dqApi.disableRule).not.toHaveBeenCalled();
  });

  // ---------- 编辑 ----------

  it("点击「编辑」预填阈值/严重级别，改动后保存调用 updateRule 并刷新列表", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    await user.click(screen.getAllByRole("button", { name: /编\s*辑/ })[0]);
    const dialog = await screen.findByRole("dialog");

    const thresholdInput = within(dialog).getByRole("spinbutton", { name: /阈值/ });
    expect(thresholdInput).toHaveValue("95.00");
    fireEvent.change(thresholdInput, { target: { value: "88.5" } });

    const severitySelect = within(dialog).getByRole("combobox", { name: /严重级别/ });
    fireEvent.mouseDown(severitySelect.closest(".ant-select-selector")!);
    // 原值已是 HIGH ⇒ 必须选「中」，否则下拉根本没改也照样绿
    pickOption("中");

    await user.click(within(dialog).getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.updateRule).toHaveBeenCalledTimes(1);
    });
    expect(api.updateRule.mock.calls[0][0]).toBe(1);
    expect(api.updateRule.mock.calls[0][1]).toEqual({
      threshold: "88.5",
      severity: "MEDIUM",
    });
    // 保存成功后 onSaved → refresh()
    await waitFor(() => {
      expect(api.listRules).toHaveBeenCalledTimes(2);
    });
  });

  it("编辑弹窗点「取消」不调用 updateRule", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    await user.click(screen.getAllByRole("button", { name: /编\s*辑/ })[0]);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /取\s*消/ }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.updateRule).not.toHaveBeenCalled();
  });

  // ---------- 新建弹窗 ----------

  it("新建弹窗：选本体类后自动带出目标表并反查默认数据源", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    await user.click(screen.getByRole("button", { name: "新建结构化规则" }));
    const dialog = await screen.findByRole("dialog");

    // 选本体类（无 name 的 Select，按 Form.Item label 定位）
    openSelect(formItemByLabel(dialog, "类名"));
    pickOption("PO_LINE (PO_LINE)");

    await waitFor(() => {
      expect(selectedText(formItemByLabel(dialog, "目标表"))).toBe("PO_LINE");
    });
    // 类名 → 默认数据源反查（THBI 是 isDefault）
    expect(selectedText(formItemByLabel(dialog, "数据源"))).toBe("THBI (ORACLE)");
  });

  it("新建弹窗：结构化模式提交后 createRule 收到完整 payload", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    await user.click(screen.getByRole("button", { name: "新建结构化规则" }));
    const dialog = await screen.findByRole("dialog");

    // 1) 选数据源 → 触发 schema 加载
    openSelect(formItemByLabel(dialog, "数据源"));
    pickOption("ODS (POSTGRESQL)");
    await waitFor(() => {
      expect(datasourceApi.getDatasourceSchema).toHaveBeenCalledWith(2);
    });

    // 2) 选目标表
    openSelect(formItemByLabel(dialog, "目标表"));
    pickOption("ODS_SUPPLIER");

    // 3) 选本体类（按 source_table 过滤后只剩 SUPPLIER）
    openSelect(formItemByLabel(dialog, "类名"));
    pickOption("SUPPLIER (ODS_SUPPLIER)");

    // 4) 选目标列
    const columnSelect = within(dialog).getByRole("combobox", { name: /目标列/ });
    fireEvent.mouseDown(columnSelect.closest(".ant-select-selector")!);
    pickOption("SUPPLIER_ID (NUMBER)");

    // 5) 选规则类型
    const ruleTypeSelect = within(dialog).getByRole("combobox", { name: /规则类型/ });
    fireEvent.mouseDown(ruleTypeSelect.closest(".ant-select-selector")!);
    pickOption("有效性 (VALIDITY)");

    // 6) 阈值
    const thresholdInput = within(dialog).getByRole("spinbutton", { name: /阈值/ });
    fireEvent.change(thresholdInput, { target: { value: "90" } });

    // 7) 严重级别
    const severitySelect = within(dialog).getByRole("combobox", { name: /严重级别/ });
    fireEvent.mouseDown(severitySelect.closest(".ant-select-selector")!);
    pickOption("中");

    // 8) 结构化参数（VALIDITY 默认 kind=range → 最小值/最大值）
    const minInput = within(formItemByLabel(dialog, "最小值")).getByRole("spinbutton");
    fireEvent.change(minInput, { target: { value: "1" } });
    const maxInput = within(formItemByLabel(dialog, "最大值")).getByRole("spinbutton");
    fireEvent.change(maxInput, { target: { value: "50" } });

    // 9) 提交
    await user.click(within(dialog).getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createRule).toHaveBeenCalledTimes(1);
    });
    expect(api.createRule.mock.calls[0][0]).toEqual({
      ruleCode: "DQ-Rule-20260915-0000000001",
      ruleName: "ODS-SUPPLIER-Validity",
      ruleType: "VALIDITY",
      targetTable: "ODS_SUPPLIER",
      targetColumn: "SUPPLIER_ID",
      datasourceId: 2,
      threshold: "90",
      severity: "MEDIUM",
      ruleParams: { kind: "range", min: 1, max: 50 },
      ruleExpression: null,
    });
    // 创建成功后 onCreated → refresh()
    await waitFor(() => {
      expect(api.listRules).toHaveBeenCalledTimes(2);
    });
  });

  it("新建弹窗：切到自定义 SQL 模式后提交，payload 带表达式且不带结构化参数", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    await user.click(screen.getByRole("button", { name: "新建结构化规则" }));
    const dialog = await screen.findByRole("dialog");

    // 切模式 → 自定义 SQL
    const modeSwitch = within(dialog).getByRole("switch");
    await user.click(modeSwitch);

    // 结构化参数表单被替换成规则表达式输入框
    expect(within(dialog).getByRole("textbox", { name: /规则表达式/ })).toBeInTheDocument();

    openSelect(formItemByLabel(dialog, "数据源"));
    pickOption("THBI (ORACLE)");
    await waitFor(() => {
      expect(datasourceApi.getDatasourceSchema).toHaveBeenCalledWith(1);
    });

    openSelect(formItemByLabel(dialog, "类名"));
    pickOption("PO_LINE (PO_LINE)");

    const ruleTypeSelect = within(dialog).getByRole("combobox", { name: /规则类型/ });
    fireEvent.mouseDown(ruleTypeSelect.closest(".ant-select-selector")!);
    pickOption("完整性 (COMPLETENESS)");

    const thresholdInput = within(dialog).getByRole("spinbutton", { name: /阈值/ });
    fireEvent.change(thresholdInput, { target: { value: "100" } });

    const severitySelect = within(dialog).getByRole("combobox", { name: /严重级别/ });
    fireEvent.mouseDown(severitySelect.closest(".ant-select-selector")!);
    pickOption("高");

    const expressionInput = within(dialog).getByRole("textbox", { name: /规则表达式/ });
    await user.type(expressionInput, "ORDER_QTY > 0");

    await user.click(within(dialog).getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createRule).toHaveBeenCalledTimes(1);
    });
    const payload = api.createRule.mock.calls[0][0];
    expect(payload.ruleExpression).toBe("ORDER_QTY > 0");
    expect(payload.ruleParams).toBeNull();
    expect(payload.ruleType).toBe("COMPLETENESS");
    expect(payload.datasourceId).toBe(1);
  });

  it("新建弹窗点「取消」不调用 createRule", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("THBI");

    await user.click(screen.getByRole("button", { name: "新建结构化规则" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /取\s*消/ }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.createRule).not.toHaveBeenCalled();
  });

  // ---------- 分页 ----------

  it("分页「每页条数」切到 50 后表格渲染全部行", async () => {
    const manyRows = Array.from({ length: 25 }, (_, i) => ({
      ...RULE_ROWS[0],
      id: i + 1,
      ruleCode: `DQ-Rule-20260915-${String(i + 1).padStart(10, "0")}`,
      ruleName: `RULE_${i + 1}`,
    }));
    api.listRules.mockResolvedValue(manyRows);

    renderPage();
    await screen.findByText("RULE_1");
    // 默认每页 20 条 → 只渲染 20 行
    expect(document.querySelectorAll(".ant-table-tbody tr.ant-table-row").length).toBe(20);

    const sizeChanger = document.querySelector(
      ".ant-pagination-options-size-changer .ant-select-selector",
    );
    if (!sizeChanger) throw new Error("pagination size changer not found");
    fireEvent.mouseDown(sizeChanger);
    pickOption("50 条/页");

    await waitFor(() => {
      expect(document.querySelectorAll(".ant-table-tbody tr.ant-table-row").length).toBe(25);
    });
  });
});
