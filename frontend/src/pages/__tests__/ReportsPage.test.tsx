/**
 * ReportsPage（A8 M4 Report 模板 MVP）行为测试。
 *
 * 覆盖 brief §验收前端部分：
 * 1. 模板选择器 + 参数表单按 paramsSchema 动态渲染
 * 2. 生成按钮提交 templateCode + params（成功提示 + 列表刷新）
 * 3. 列表状态徽标（PENDING_REVIEW=orange Tag 文案「待审批」）
 * 4. admin 看到 PENDING 行的「审批」按钮 → 弹窗 → 提交 reviewReport(APPROVE)
 * 5. 非 admin 无审批按钮
 *
 * 模式参考 WikiLinksPage.test.tsx：vi.hoisted mock + I18nextProvider +
 * ConfigProvider 包装；RTL 锚点用 UI 信号（按钮/Modal 出现），不用 mock
 * 参数匹配（跨测试遗留 promise 教训）。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import ReportsPage from "../ReportsPage";
import type { ReportInstance, ReportTemplate } from "../../types/reports";

const api = vi.hoisted(() => ({
  listReportTemplates: vi.fn(),
  generateReport: vi.fn(),
  listReports: vi.fn(),
  getReport: vi.fn(),
  reviewReport: vi.fn(),
}));

const auth = vi.hoisted(() => ({
  useAuthStore: vi.fn((selector: (s: { user: { roles: string[] } | null }) => unknown) =>
    selector({ user: { roles: ["admin"] } }),
  ),
}));

vi.mock("../../api/reports", () => api);
vi.mock("../../stores/authStore", () => auth);

const TEMPLATES: ReportTemplate[] = [
  {
    code: "monthly-ops-v1",
    title: "月度经营分析报告",
    description: "月度 KPI 总览",
    paramsSchema: [
      { name: "month", type: "str", required: true, pattern: "^\\d{4}-\\d{2}$" },
      { name: "supplierKey", type: "str", required: true, pattern: null },
    ],
  },
  {
    code: "supplier-360-v1",
    title: "供应商 360° 报告",
    description: "供应商档案",
    paramsSchema: [{ name: "supplierKey", type: "str", required: true, pattern: null }],
  },
];

const INSTANCE: ReportInstance = {
  id: 42,
  templateCode: "monthly-ops-v1",
  title: "月度经营分析报告 · 2026-09 · 10105",
  params: { month: "2026-09", supplierKey: "10105" },
  sections: [
    {
      sectionId: "kpi-overview",
      title: "月度 KPI 总览",
      kind: "kpi_cards",
      data: [{ kpiCode: "KPI_SUPPLIER_OTD", kpiName: "OTD", found: true }],
      renderError: null,
    },
    {
      sectionId: "kpi-definitions",
      title: "KPI 口径说明",
      kind: "table",
      data: [
        { kpiCode: "KPI_SUPPLIER_OTD", kpiName: "OTD", formula: "SELECT 1" },
      ],
      renderError: null,
    },
    {
      sectionId: "supplier-kpis",
      title: "供应商特征值",
      kind: "table",
      data: null,
      renderError: "供应商不存在",
    },
  ],
  summary: "[事实] 9 月 OTD 达标\n下月预计回升\n[推断] 由数据推演\n[假设] 或有反复",
  status: "PENDING_REVIEW",
  reviewNote: null,
  reviewedBy: null,
  createdBy: "alice",
  createdTime: "2026-09-29T10:00:00+08:00",
  updatedTime: "2026-09-29T10:00:00+08:00",
};

const LIST_ROW = {
  id: 42,
  templateCode: "monthly-ops-v1",
  title: INSTANCE.title,
  status: "PENDING_REVIEW" as const,
  createdBy: "alice",
  createdTime: INSTANCE.createdTime,
};

function renderPage() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <ReportsPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
}

describe("ReportsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listReportTemplates.mockResolvedValue(TEMPLATES);
    api.listReports.mockResolvedValue({ rows: [LIST_ROW], total: 1 });
    api.getReport.mockResolvedValue(INSTANCE);
    api.generateReport.mockResolvedValue(INSTANCE);
    api.reviewReport.mockResolvedValue({ ...INSTANCE, status: "APPROVED" });
  });

  it("渲染模板选择器 + 动态参数表单 + 列表状态徽标", async () => {
    renderPage();
    // 模板 select 出现（默认选中第一个模板）
    await waitFor(() => expect(api.listReportTemplates).toHaveBeenCalled());
    // 动态参数表单：paramsSchema 两个字段都有输入框
    expect(document.getElementById("report-param-month")).not.toBeNull();
    expect(document.getElementById("report-param-supplierKey")).not.toBeNull();
    // 列表加载 + PENDING_REVIEW 徽标文案
    await waitFor(() => expect(screen.getByText("待审批")).toBeInTheDocument());
    expect(screen.getByText("月度经营分析报告 · 2026-09 · 10105")).toBeInTheDocument();
  });

  it("生成按钮提交 templateCode + 校验过的参数", async () => {
    renderPage();
    await waitFor(() =>
      expect(document.getElementById("report-param-month")).not.toBeNull(),
    );
    fireEvent.change(document.getElementById("report-param-month")!, {
      target: { value: "2026-09" },
    });
    fireEvent.change(document.getElementById("report-param-supplierKey")!, {
      target: { value: "10105" },
    });
    fireEvent.click(screen.getByRole("button", { name: /生成报告/ }));
    await waitFor(() =>
      expect(api.generateReport).toHaveBeenCalledWith({
        templateCode: "monthly-ops-v1",
        params: { month: "2026-09", supplierKey: "10105" },
      }),
    );
  });

  it("必填参数缺失时生成按钮不提交", async () => {
    renderPage();
    await waitFor(() =>
      expect(document.getElementById("report-param-month")).not.toBeNull(),
    );
    fireEvent.click(screen.getByRole("button", { name: /生成报告/ }));
    await waitFor(() => expect(api.generateReport).not.toHaveBeenCalled());
  });

  it("admin 对 PENDING 行审批：弹窗 + 提交 reviewReport(APPROVE)", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("待审批")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /审\s*批/ }));
    // Modal 出现（用 .ant-modal-title 锁定，避免与页面标题重名——antd RTL 坑）
    await waitFor(() =>
      expect(document.querySelector(".ant-modal-title")?.textContent).toContain("42"),
    );
    fireEvent.click(screen.getByRole("button", { name: /提\s*交\s*审\s*批/ }));
    await waitFor(() =>
      expect(api.reviewReport).toHaveBeenCalledWith(42, {
        decision: "APPROVE",
        note: null,
      }),
    );
  });

  it("非 admin 看不到审批按钮", async () => {
    auth.useAuthStore.mockImplementation(
      (selector: (s: { user: { roles: string[] } | null }) => unknown) =>
        selector({ user: { roles: ["user"] } }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByText("待审批")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /审\s*批/ })).toBeNull();
  });

  it("详情 Drawer：table / kpi_cards / renderError 三分支渲染", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("待审批")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /查\s*看/ }));
    // 等详情加载完成（kpi 卡片标题来自 kpi_cards 分支）
    await waitFor(() =>
      expect(document.querySelector(".ant-drawer-open")).not.toBeNull(),
    );
    // kpi_cards 分支：卡片标题 = kpiName（"OTD" 同时出现在 table 分支，用卡片头锁定）
    await waitFor(() => expect(screen.getByText("月度 KPI 总览")).toBeInTheDocument());
    expect(
      document.querySelector(".ant-card-head-title")?.textContent,
    ).toContain("OTD");
    // table 分支：antd Table 渲染出列名 + 单元格
    expect(screen.getByText("KPI 口径说明")).toBeInTheDocument();
    expect(screen.getByText("SELECT 1")).toBeInTheDocument();
    // renderError 分支：Alert + 错误详情
    expect(screen.getByText("该分节数据绑定失败")).toBeInTheDocument();
    expect(screen.getByText("供应商不存在")).toBeInTheDocument();
  });

  it("总结块按 [事实]/[推断]/[假设] 前缀着色（真实分类逻辑，无前缀兜底 [推断]）", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("待审批")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /查\s*看/ }));
    await waitFor(() => expect(screen.getByText("总结")).toBeInTheDocument());

    const tagClassesOf = (prefix: string): string[] =>
      screen
        .getAllByText(prefix)
        .map((el) => el.closest(".ant-tag")?.className ?? "");

    expect(tagClassesOf("[事实]")[0]).toContain("ant-tag-green");
    // 无前缀行「下月预计回升」被兜底为 [推断]（orange）；显式 [推断] 行同色
    const inferenceTags = tagClassesOf("[推断]");
    expect(inferenceTags.length).toBe(2);
    for (const cls of inferenceTags) {
      expect(cls).toContain("ant-tag-orange");
    }
    expect(tagClassesOf("[假设]")[0]).toContain("ant-tag-purple");
    // 原始行文本剥掉前缀后渲染
    expect(screen.getByText("9 月 OTD 达标")).toBeInTheDocument();
    expect(screen.getByText("下月预计回升")).toBeInTheDocument();
    expect(screen.getByText("由数据推演")).toBeInTheDocument();
    expect(screen.getByText("或有反复")).toBeInTheDocument();
  });
});
