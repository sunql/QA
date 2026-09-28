/** DataQualityRuleGeneratePage — 全选按钮回归。
 *
 * 背景：建议规则常达数十条，单条勾选体验差。加 全选 / 取消全选 一键切换。
 * 范围：仅操作 status="NEW" 的建议；EXISTS（已落库）规则本来就不能再次落库，
 *        不应受全选按钮影响——避免用户误以为能再次确认。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { i18n } from "../i18n";
import DataQualityRuleGeneratePage from "../pages/DataQualityRuleGeneratePage";

// 后端 API mock：listClasses / listDataSources / previewRules / listLlmModels
const previewMock = vi.hoisted(() => vi.fn());
const listClassesMock = vi.hoisted(() => vi.fn());
const listDataSourcesMock = vi.hoisted(() => vi.fn());
const listLlmModelsMock = vi.hoisted(() => vi.fn());

vi.mock("../api/dataQualityGenerate", async (importOriginal) => {
  const a = await importOriginal<typeof import("../api/dataQualityGenerate")>();
  return {
    ...a,
    previewRules: previewMock,
    listLlmModels: listLlmModelsMock,
    confirmRules: vi.fn(),
    parseDescriptions: vi.fn(),
    applySuggestion: vi.fn(),
  };
});
vi.mock("../api/ontology", async (importOriginal) => {
  const a = await importOriginal<typeof import("../api/ontology")>();
  return { ...a, listClasses: listClassesMock };
});
vi.mock("../api/datasource", async (importOriginal) => {
  const a = await importOriginal<typeof import("../api/datasource")>();
  return { ...a, listDataSources: listDataSourcesMock };
});

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <ConfigProvider locale={zhCN}>
    <I18nextProvider i18n={i18n}>
      <MemoryRouter>{children}</MemoryRouter>
    </I18nextProvider>
  </ConfigProvider>
);

// 3 NEW + 2 EXISTS，共 5 条建议
const previewResponse = {
  classId: 4,
  className: "Supplier",
  sourceTable: "ODS_BPSUPPLIER",
  datasourceId: 1,
  suggestions: [
    { ruleCode: "DQ_A", ruleName: "A", ruleType: "COMPLETENESS", targetTable: "ODS_BPSUPPLIER",
      targetColumn: "X", ruleExpression: "X IS NOT NULL", threshold: 100, severity: "HIGH",
      derivationType: "NOT_NULL", sourcePropertyId: 1, sourceClassId: 4,
      confidence: "HIGH", status: "NEW", reason: "非空约束" },
    { ruleCode: "DQ_B", ruleName: "B", ruleType: "COMPLETENESS", targetTable: "ODS_BPSUPPLIER",
      targetColumn: "Y", ruleExpression: "Y IS NOT NULL", threshold: 100, severity: "HIGH",
      derivationType: "NOT_NULL", sourcePropertyId: 2, sourceClassId: 4,
      confidence: "HIGH", status: "NEW", reason: "非空约束" },
    { ruleCode: "DQ_C", ruleName: "C", ruleType: "UNIQUENESS", targetTable: "ODS_BPSUPPLIER",
      targetColumn: "Z", ruleExpression: "UNIQUE(Z)", threshold: 100, severity: "HIGH",
      derivationType: "PK_DERIVED", sourcePropertyId: 3, sourceClassId: 4,
      confidence: "HIGH", status: "NEW", reason: "主键唯一性" },
    { ruleCode: "DQ_D", ruleName: "D", ruleType: "COMPLETENESS", targetTable: "ODS_BPSUPPLIER",
      targetColumn: "M", ruleExpression: "M IS NOT NULL", threshold: 100, severity: "HIGH",
      derivationType: "NOT_NULL", sourcePropertyId: 4, sourceClassId: 4,
      confidence: "HIGH", status: "EXISTS", reason: "非空约束" },
    { ruleCode: "DQ_E", ruleName: "E", ruleType: "VALIDITY", targetTable: "ODS_BPSUPPLIER",
      targetColumn: "N", ruleExpression: "N IN ('1','2')", threshold: 95, severity: "MEDIUM",
      derivationType: "ALLOWED_VALUES", sourcePropertyId: 5, sourceClassId: 4,
      confidence: "HIGH", status: "EXISTS", reason: "固定值域" },
  ],
  blocked: [],
};

/** 打开 antd Select 的下拉：项目内既定模式是 mouseDown .ant-select-selector，
 *  再 click .ant-select-item-option-content（role=option 不可点）。 */
function openSelect() {
  const selector = document.querySelector(".ant-select-selector");
  if (!selector) throw new Error("antd Select not found");
  fireEvent.mouseDown(selector);
}
function pickOption(text: string | RegExp) {
  const opt = screen.getByText(text, {
    selector: ".ant-select-item-option-content",
  });
  fireEvent.click(opt);
}

async function gotoPreviewStep(user: ReturnType<typeof userEvent.setup>) {
  // 等到本体类列表加载完
  await waitFor(() => expect(listClassesMock).toHaveBeenCalled());
  openSelect();
  pickOption(/Supplier/);
  await user.click(screen.getByRole("button", { name: /下一步/ }));

  // 步骤 1 已卸载 → 等到 listDataSources 加载完成，再开下拉
  await waitFor(() => expect(listDataSourcesMock).toHaveBeenCalled());
  openSelect();
  pickOption("ds1");
  await user.click(screen.getByRole("button", { name: /下一步/ }));
  await waitFor(() => expect(previewMock).toHaveBeenCalled());
}

describe("DataQualityRuleGeneratePage — 全选按钮", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listClassesMock.mockResolvedValue([
      { id: 4, className: "Supplier", classAlias: "供应商", description: null,
        sourceTable: "ODS_BPSUPPLIER", parentClassId: null, objectType: "Master",
        objectOwner: null, createdBy: null, createdTime: null, updatedTime: null,
        version: 1, validFrom: null, validTo: null },
    ]);
    listDataSourcesMock.mockResolvedValue([
      { id: 1, name: "ds1", type: "POSTGRESQL", host: "h", port: 5432,
        databaseName: "d", username: "u", passwordEncrypted: "x",
        enabled: true, createdTime: null, updatedTime: null },
    ]);
    previewMock.mockResolvedValue(previewResponse);
    listLlmModelsMock.mockResolvedValue([]);
  });

  it("初次进入预览：默认全选所有 NEW（既有行为不回归）", async () => {
    const user = userEvent.setup();
    render(<DataQualityRuleGeneratePage />, { wrapper });
    await gotoPreviewStep(user);

    // 3 条 NEW 的 checkbox 应默认勾选；2 条 EXISTS 的 checkbox 不应出现
    // （EXISTS 规则既不能再确认，按现有逻辑行内 checkbox 也无意义；这里只断言 NEW 行被勾选）
    const checked = await screen.findAllByRole("checkbox", { checked: true });
    expect(checked.length).toBe(3);
  });

  it("点击「取消全选」按钮：所有 NEW 复选框变未勾选，EXISTS 不受影响", async () => {
    const user = userEvent.setup();
    render(<DataQualityRuleGeneratePage />, { wrapper });
    await gotoPreviewStep(user);

    const toggleBtn = await screen.findByRole("button", { name: /全选|取消全选/ });
    // 初次进入是「全选」状态 → 第一次点击切到「取消全选」
    await user.click(toggleBtn);

    const checked = screen.queryAllByRole("checkbox", { checked: true });
    expect(checked.length).toBe(0);
  });

  it("点两次：第一次取消全选、第二次重新全选所有 NEW", async () => {
    const user = userEvent.setup();
    render(<DataQualityRuleGeneratePage />, { wrapper });
    await gotoPreviewStep(user);

    const toggleBtn = await screen.findByRole("button", { name: /全选|取消全选/ });
    // 默认 3 NEW 全选：第一次点 → 全不选
    await user.click(toggleBtn);
    expect(screen.queryAllByRole("checkbox", { checked: true }).length).toBe(0);
    // 第二次点 → 恢复 3 NEW 全选
    await user.click(toggleBtn);
    expect((await screen.findAllByRole("checkbox", { checked: true })).length).toBe(3);
  });
});