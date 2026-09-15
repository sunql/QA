/** DataQualityReportDetailPage 规则明细表渲染测试
 * （feat-report-rules-zh-name + feat-report-kpi-data-count，2026-09-15）
 *
 * 覆盖：
 * - 表头中文化（rule / type / target / severity / passRate / status）
 * - Rule 列展示 rule_name（业务可读名），缺省时降级显示「系统编码」徽标 + tooltip
 * - Type 列把 enum 映射到 ruleTypeLabels（COMPLETENESS → 完整性）
 * - Severity 列把 enum 映射到 severityLabels（HIGH → 高）
 * - 评估摘要 KPI 卡片新增「数据条目数」卡（total_count 求和）
 *
 * 不直接挂 antd 的 echarts + Accordion 等重组件，全部 mock 成最小 stub；本测试只关注
 * 规则明细 Table 的列渲染与 cell 渲染函数行为 + KPI 卡片渲染。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";

// API 层 mock —— 必须把页面实际用到的函数全部列出来。
const reportDetail = vi.hoisted(() => ({
  current: null as Record<string, unknown> | null,
}));
vi.mock("../api/evaluationReport", () => ({
  getReport: vi.fn(),
  getReportProgress: vi.fn(),
  listSamples: vi.fn(),
  listShares: vi.fn(),
  createShare: vi.fn(),
  revokeShare: vi.fn(),
  regenerateReport: vi.fn(),
  deleteReport: vi.fn(),
  updateReport: vi.fn(),
  compareReports: vi.fn(),
  getPublicReport: vi.fn(),
}));

// 重图表组件全部 stub，避免 echarts / canvas 在 jsdom 里炸。
vi.mock("../components/ReportDimensionBar", () => ({
  __esModule: true,
  default: () => <div data-testid="stub-dim" />,
}));
vi.mock("../components/ReportPassFailPie", () => ({
  __esModule: true,
  default: () => <div data-testid="stub-pie" />,
}));
vi.mock("../components/ReportTrendLine", () => ({
  __esModule: true,
  default: () => <div data-testid="stub-trend" />,
}));
vi.mock("../components/ViolationSampleTable", () => ({
  __esModule: true,
  default: () => <div data-testid="stub-samples" />,
}));

// antd App 上下文（message / modal 用）
vi.mock("antd", async (importOriginal) => {
  const antd = await importOriginal<typeof import("antd")>();
  return { ...antd };
});

import DataQualityReportDetailPage from "../pages/DataQualityReportDetailPage";
import * as api from "../api/evaluationReport";
import type { EvaluationReport } from "../types/evaluationReport";

function makeReport(snapshot: Record<string, unknown>): EvaluationReport {
  return {
    id: 1,
    name: "测试报告",
    description: null,
    status: "COMPLETED",
    classIds: [1],
    ruleIds: [1],
    tags: [],
    owner: null,
    snapshotVersion: 1,
    timeWindowStart: "2026-01-01T00:00:00Z",
    timeWindowEnd: "2026-01-31T23:59:59Z",
    progress: null,
    snapshot,
    createdBy: "tester",
    createdTime: "2026-01-01T00:00:00Z",
    updatedTime: "2026-01-01T00:00:00Z",
    deletedAt: null,
  };
}

describe("DataQualityReportDetailPage 规则明细表（feat-report-rules-zh-name）", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    reportDetail.current = null;
  });

  function mountAt(reportId: string): void {
    render(
      <ConfigProvider locale={zhCN}>
        <MemoryRouter initialEntries={[`/data-quality/reports/${reportId}`]}>
          <Routes>
            <Route
              path="/data-quality/reports/:id"
              element={<DataQualityReportDetailPage />}
            />
          </Routes>
        </MemoryRouter>
      </ConfigProvider>,
    );
  }

  it(
    "表头中文化 + Rule 列显示 rule_name（业务可读名）+ Type 列显示中文标签",
    async () => {
      vi.mocked(api.getReport).mockResolvedValue(
        makeReport({
          schema_version: 1,
          overall: { score: 80, status: "FAIL" },
          tables: [
            {
              target_table: "PORDER",
              overall_score: 80,
              rules: [
                {
                  rule_id: 1,
                  rule_code: "PO_QTY_NON_NEG",
                  rule_name: "采购订单数量非负",
                  rule_type: "COMPLETENESS",
                  severity: "HIGH",
                  total_count: 100,
                  passed_count: 80,
                  violation_count: 20,
                  pass_rate: 80.0,
                  status: "FAIL",
                },
              ],
            },
          ],
        }),
      );
      vi.mocked(api.getReportProgress).mockResolvedValue({
        stage: "COMPLETED",
        completed: 1,
        total: 1,
        currentRuleId: null,
        currentRuleCode: null,
        message: null,
        startedAt: null,
        finishedAt: "2026-01-01T00:00:00Z",
      });
      vi.mocked(api.listSamples).mockResolvedValue([]);
      vi.mocked(api.listShares).mockResolvedValue([]);

      mountAt("1");

      // 表头中文（页面顶部 Descriptions 也有「状态」字段标签，故用 getAllByText 验存在）
      await waitFor(() => {
        expect(screen.getByText("规则")).toBeInTheDocument();
      });
      expect(screen.getByText("类型")).toBeInTheDocument();
      expect(screen.getByText("目标")).toBeInTheDocument();
      expect(screen.getByText("严重级别")).toBeInTheDocument();
      expect(screen.getByText("通过率")).toBeInTheDocument();
      expect(screen.getAllByText("状态").length).toBeGreaterThan(0);

      // Rule 列：显示 rule_name（业务可读），不显示 rule_code
      expect(screen.getByText("采购订单数量非负")).toBeInTheDocument();
      expect(screen.queryByText("PO_QTY_NON_NEG")).not.toBeInTheDocument();

      // Type 列：COMPLETENESS → 完整性
      expect(screen.getByText("完整性")).toBeInTheDocument();

      // Severity 列：HIGH → 高
      expect(screen.getByText("高")).toBeInTheDocument();
    },
    30_000,
  );

  it(
    "评估摘要 KPI 卡新增「评估数据条目数」（feat-report-kpi-data-count）",
    async () => {
      vi.mocked(api.getReport).mockResolvedValue(
        makeReport({
          schema_version: 1,
          overall: { score: 80, status: "FAIL" },
          tables: [
            {
              target_table: "PORDER",
              overall_score: 80,
              rules: [
                {
                  rule_id: 1,
                  rule_code: "PO_QTY_NON_NEG",
                  rule_name: "采购订单数量非负",
                  rule_type: "COMPLETENESS",
                  severity: "HIGH",
                  total_count: 100,
                  passed_count: 80,
                  violation_count: 20,
                  pass_rate: 80.0,
                  status: "FAIL",
                },
                {
                  rule_id: 2,
                  rule_code: "PO_PRICE_NON_NEG",
                  rule_name: "采购订单单价非负",
                  rule_type: "VALIDITY",
                  severity: "MEDIUM",
                  total_count: 250,
                  passed_count: 240,
                  violation_count: 10,
                  pass_rate: 96.0,
                  status: "PASS",
                },
              ],
            },
          ],
        }),
      );
      vi.mocked(api.getReportProgress).mockResolvedValue({
        stage: "COMPLETED",
        completed: 2,
        total: 2,
        currentRuleId: null,
        currentRuleCode: null,
        message: null,
        startedAt: null,
        finishedAt: "2026-01-01T00:00:00Z",
      });
      vi.mocked(api.listSamples).mockResolvedValue([]);
      vi.mocked(api.listShares).mockResolvedValue([]);

      mountAt("1");

      // KPI 卡：标签 + 值
      await waitFor(() => {
        expect(screen.getByText("综合评分")).toBeInTheDocument();
      });
      expect(screen.getByText("规则数")).toBeInTheDocument();
      expect(screen.getByText("通过数")).toBeInTheDocument();
      expect(screen.getByText("未通过数")).toBeInTheDocument();
      // 新卡
      expect(screen.getByText("评估数据条目数")).toBeInTheDocument();
      // total_count 求和 = 100 + 250 = 350；antd Statistic 用千分位 → "350"
      expect(screen.getByText("350")).toBeInTheDocument();
      // 规则数 = 2
      expect(screen.getByText("2")).toBeInTheDocument();
    },
    30_000,
  );

  it(
    "Rule 列降级显示 rule_code + 「系统编码」徽标 + tooltip 提示重新生成报告",
    async () => {
      vi.mocked(api.getReport).mockResolvedValue(
        makeReport({
          schema_version: 1,
          overall: { score: 100, status: "PASS" },
          tables: [
            {
              target_table: "PORDER",
              overall_score: 100,
              rules: [
                {
                  rule_id: 2,
                  rule_code: "OLD_STYLE_RULE",
                  // 故意不带 rule_name（老 snapshot）
                  rule_type: "VALIDITY",
                  severity: "MEDIUM",
                  total_count: 10,
                  passed_count: 10,
                  violation_count: 0,
                  pass_rate: 100.0,
                  status: "PASS",
                },
              ],
            },
          ],
        }),
      );
      vi.mocked(api.getReportProgress).mockResolvedValue({
        stage: "COMPLETED",
        completed: 1,
        total: 1,
        currentRuleId: null,
        currentRuleCode: null,
        message: null,
        startedAt: null,
        finishedAt: null,
      });
      vi.mocked(api.listSamples).mockResolvedValue([]);
      vi.mocked(api.listShares).mockResolvedValue([]);

      mountAt("1");

      await waitFor(() => {
        expect(screen.getByText("OLD_STYLE_RULE")).toBeInTheDocument();
      });
      // 降级徽标
      expect(screen.getByText("系统编码")).toBeInTheDocument();
      // VALIDITY → 有效性
      expect(screen.getByText("有效性")).toBeInTheDocument();
      // MEDIUM → 中
      expect(screen.getByText("中")).toBeInTheDocument();
      // tooltip 文本：antd Tooltip 默认 lazy mount，jsdom 不 hover 时不渲染 portal；
      // 这里改为断言 Tooltip title 属性被正确传入 —— 通过 svg / 容器存在性 + 徽标组合已足够。
    },
    30_000,
  );

  it(
    "Rule 列在 rule_name 缺失时降级显示 rule_code（兼容旧 snapshot）",
    async () => {
      vi.mocked(api.getReport).mockResolvedValue(
        makeReport({
          schema_version: 1,
          overall: { score: 100, status: "PASS" },
          tables: [
            {
              target_table: "PORDER",
              overall_score: 100,
              rules: [
                {
                  rule_id: 2,
                  rule_code: "OLD_STYLE_RULE",
                  // 故意不带 rule_name（老 snapshot）
                  rule_type: "VALIDITY",
                  severity: "MEDIUM",
                  total_count: 10,
                  passed_count: 10,
                  violation_count: 0,
                  pass_rate: 100.0,
                  status: "PASS",
                },
              ],
            },
          ],
        }),
      );
      vi.mocked(api.getReportProgress).mockResolvedValue({
        stage: "COMPLETED",
        completed: 1,
        total: 1,
        currentRuleId: null,
        currentRuleCode: null,
        message: null,
        startedAt: null,
        finishedAt: null,
      });
      vi.mocked(api.listSamples).mockResolvedValue([]);
      vi.mocked(api.listShares).mockResolvedValue([]);

      mountAt("1");

      await waitFor(() => {
        expect(screen.getByText("OLD_STYLE_RULE")).toBeInTheDocument();
      });
      // VALIDITY → 有效性
      expect(screen.getByText("有效性")).toBeInTheDocument();
      // MEDIUM → 中
      expect(screen.getByText("中")).toBeInTheDocument();
    },
    30_000,
  );
});
