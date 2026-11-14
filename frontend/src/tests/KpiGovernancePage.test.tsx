import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import KpiGovernancePage from "../pages/KpiGovernancePage";
import { useAuthStore } from "../stores/authStore";
import type { AuthMeRead } from "../types/auth";
import type { KpiGovernanceSummary } from "../types/kpiGovernance";
import * as api from "../api/kpiGovernance";

vi.mock("../api/kpiGovernance");

/** 形状照 ClassTabAdminSync.test.tsx 的 AuthMeRead 构造。 */
const ADMIN_USER: AuthMeRead = {
  id: 1,
  username: "admin",
  displayName: "管理员",
  email: null,
  enabled: true,
  mustChangePassword: false,
  roles: ["admin"],
  organizations: [],
  tenantId: "default",
  lastLoginAt: null,
};

const NORMAL_USER: AuthMeRead = {
  ...ADMIN_USER,
  id: 2,
  username: "alice",
  displayName: "普通用户",
  roles: ["user"],
};

/**
 * 计数刻意取互不相同的值：若页面把某个字段接错（例如把 milvusKpiCount
 * 渲染到别的卡片），按 testid 断言就会失败，而不是被相同的 8 掩盖。
 */
const SUMMARY: KpiGovernanceSummary = {
  kpis: { total: 12, byStatus: { DRAFT: 3, PUBLISHED: 8, DEPRECATED: 1 } },
  metrics: {
    total: 13,
    byExecutionSource: { COMPILED: 6, ESCAPE_HATCH: 5, NONE: 2 },
    byStatus: { DRAFT: 4, PUBLISHED: 9 },
  },
  reconcile: {
    windowDays: 7,
    mismatchCount: 2,
    passCount: 40,
    unknownCount: 1,
    lastMismatchAt: "2026-11-13T10:00:00Z",
    reconcileError: null,
  },
  syncHealth: {
    publishedKpiCount: 8,
    milvusKpiCount: 7,
    kpiTotal: 12,
    neo4jKpiCount: 11,
    syncHealthError: null,
  },
  gates: { publishGateEnabled: false, tierAEnabled: true, syncEnabled: true },
  generatedAt: "2026-11-14T00:00:00Z",
};

/** 降级态：外部审计/同步健康查询失败，且 Milvus 计数为 null。 */
const DEGRADED_SUMMARY: KpiGovernanceSummary = {
  ...SUMMARY,
  reconcile: { ...SUMMARY.reconcile, reconcileError: "audit down" },
  syncHealth: {
    ...SUMMARY.syncHealth,
    milvusKpiCount: null,
    syncHealthError: "milvus down",
  },
};

describe("KpiGovernancePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getKpiGovernanceSummary).mockResolvedValue(SUMMARY);
    useAuthStore.setState({ user: ADMIN_USER });
  });

  it("渲染 KPI 状态、执行来源与三库同步计数", async () => {
    render(<KpiGovernancePage />);

    // 等首屏数据落地
    expect(await screen.findByTestId("stat-kpi-total")).toHaveTextContent("12");
    // 执行来源分布（标签而非裸数字）
    expect(screen.getByText(/COMPILED/)).toBeInTheDocument();
    expect(screen.getByTestId("stat-metric-source-ESCAPE_HATCH")).toHaveTextContent("5");
    // milvusKpiCount=7 / publishedKpiCount=8 / neo4jKpiCount=11 —— 各自卡片
    expect(screen.getByTestId("stat-milvus-count")).toHaveTextContent("7");
    expect(screen.getByTestId("stat-published-count")).toHaveTextContent("8");
    expect(screen.getByTestId("stat-neo4j-count")).toHaveTextContent("11");
    // 对账口径：mismatchCount 与 passCount 分开显示
    expect(screen.getByTestId("stat-mismatch-count")).toHaveTextContent("2");
    expect(screen.getByTestId("stat-pass-count")).toHaveTextContent("40");
  });

  it("加载失败给出可读提示，重试后恢复数据并清除错误", async () => {
    vi.mocked(api.getKpiGovernanceSummary).mockRejectedValueOnce(new Error("boom"));
    render(<KpiGovernancePage />);

    expect(await screen.findByText(/加载失败/)).toBeInTheDocument();
    // 失败态不得留一个无限转圈
    expect(screen.queryByTestId("governance-loading")).not.toBeInTheDocument();

    // 重试按钮走同一个 loader：数据出现即证明恢复到正常渲染
    vi.mocked(api.getKpiGovernanceSummary).mockResolvedValue(SUMMARY);
    // antd Button 会在两个汉字之间插入空格（「重 试」），用正则匹配
    fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
    expect(await screen.findByTestId("stat-kpi-total")).toHaveTextContent("12");
    // 成功的重试必须清掉旧的错误提示
    expect(screen.queryByText(/加载失败/)).not.toBeInTheDocument();
  });

  it("降级错误串渲染为本地化文案，null 计数显示占位符而不是 0", async () => {
    vi.mocked(api.getKpiGovernanceSummary).mockResolvedValue(DEGRADED_SUMMARY);
    render(<KpiGovernancePage />);

    // 键缺失时 t() 回退成 key 本身 —— 这两条断言会因此变红
    expect(
      await screen.findByText(/同步健康查询失败：milvus down/),
    ).toBeInTheDocument();
    expect(screen.getByText(/对账查询失败：audit down/)).toBeInTheDocument();

    // best-effort 的 null 计数是「取不到」，不能伪装成 0
    expect(screen.getByTestId("stat-milvus-count")).toHaveTextContent("—");
    expect(screen.getByTestId("stat-milvus-count")).not.toHaveTextContent("0");
  });

  it("闸门卡片按开关值显示开启/关闭", async () => {
    render(<KpiGovernancePage />);

    expect(await screen.findByTestId("gate-publishGate")).toHaveTextContent("关闭");
    expect(screen.getByTestId("gate-tierA")).toHaveTextContent("开启");
    expect(screen.getByTestId("gate-syncGate")).toHaveTextContent("开启");
    expect(screen.getByTestId("gate-publishGate")).not.toHaveTextContent("开启");
  });

  it("auth 用户尚未恢复时按 loading 处理，不误报仅管理员可见", () => {
    useAuthStore.setState({ user: null });
    render(<KpiGovernancePage />);

    expect(screen.getByTestId("governance-loading")).toBeInTheDocument();
    expect(screen.queryByText("仅管理员可见")).not.toBeInTheDocument();
    expect(api.getKpiGovernanceSummary).not.toHaveBeenCalled();
  });

  it("非 admin 只显示提示且不请求接口", async () => {
    useAuthStore.setState({ user: NORMAL_USER });
    render(<KpiGovernancePage />);

    expect(await screen.findByText("仅管理员可见")).toBeInTheDocument();
    expect(api.getKpiGovernanceSummary).not.toHaveBeenCalled();
  });
});
