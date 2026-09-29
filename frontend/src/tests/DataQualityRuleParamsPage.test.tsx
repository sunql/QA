/** DataQualityRuleParamsPage — 规则参数结构化配置页（feat-dq-rule-params Task 11）
 * 2026-09-15：列表列头中文 + 新建弹窗（自动编码/自动命名/级联选择）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
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

vi.mock("../api/dataQualityRuleParams", () => api);
vi.mock("../api/ontology", () => ontologyApi);
vi.mock("../api/datasource", () => datasourceApi);

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
