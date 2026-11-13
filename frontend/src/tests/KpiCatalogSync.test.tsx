/**
 * KPI 目录页 admin 同步入口（页头全量对账 + 行级重新同步）。
 *
 * 从 `KpiCatalogPage.test.tsx` 拆出（该文件曾因追加本组用例涨到 896 行 >
 * 800 行硬顶）。vitest 的 `vi.mock` / `vi.hoisted` 是文件级作用域、不能跨文件
 * import ⇒ 这里自带一份最小 harness（api mock / fixture / renderPage /
 * 用户工厂 / beforeEach），断言与拆分前等价或更强。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider, message } from "antd";
import zhCN from "antd/locale/zh_CN";
import KpiCatalogPage from "../pages/KpiCatalogPage";
import { useAuthStore } from "../stores/authStore";
import type { AuthMeRead } from "../types/auth";
import type { KpiCatalog } from "../types/kpiCatalog";

const { mockKpi } = vi.hoisted(() => ({
  mockKpi: {
    id: 1,
    kpiCode: "KPI_SUPPLIER_OTD",
    kpiName: "供应商准时交付率",
    businessDefinition: "按采购订单行项按时签收比例",
    formula: "COUNT(RECEIVED_QTY<=ORDER_QTY)/COUNT(*)",
    numerator: "按时签收订单数",
    denominator: "总订单数",
    grain: "供应商+月",
    timeGrain: "month",
    unit: "%",
    dataSource: "DWS_SUPPLIER_MONTHLY",
    datasourceId: 7,
    owner: "采购部",
    version: "v1.0",
    revisionCount: 0,
    status: "PUBLISHED",
    metricId: null,
    semanticKeywords: ["准时交付"],
    matchThreshold: 0.6,
    grainKeys: ["supplier_code"],
    numeratorColumns: ["QTY"],
    denominatorColumns: ["AMOUNT"],
    createdBy: null,
    createdTime: "2026-08-30T00:00:00Z",
    updatedTime: "2026-08-30T00:00:00Z",
  } as KpiCatalog,
}));

const api = vi.hoisted(() => ({
  listKpis: vi.fn(),
  createKpi: vi.fn(),
  updateKpi: vi.fn(),
  deleteKpi: vi.fn(),
  getDatasourceSuggestions: vi.fn(),
  // 页头全量对账 / 行级重新同步
  syncMissingKpi: vi.fn(),
  resyncKpiEmbedding: vi.fn(),
}));

vi.mock("../api/kpiCatalog", () => api);

const datasourceApi = vi.hoisted(() => ({
  listDataSources: vi.fn(),
}));

vi.mock("../api/datasource", () => datasourceApi);

const ontologyApi = vi.hoisted(() => ({
  listMetrics: vi.fn(),
}));

vi.mock("../api/ontology", () => ontologyApi);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/kpi-catalog"]}>
        <Routes>
          <Route path="/kpi-catalog" element={<KpiCatalogPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

/** KPI 写入口限 admin：既有用例默认以管理员身份渲染。 */
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

const NORMAL_USER: AuthMeRead = { ...ADMIN_USER, username: "alice", roles: ["user"] };

/** 真实 zh-CN 渲染文案（`t()` 在本测试环境返回翻译文本，不是裸 key）。 */
const SYNC_ALL_NONE_MSG = "无缺失：向量与图均已是最新";
const SYNC_ALL_PARTIAL_2_MSG = "同步完成但有 2 条失败，详见后端日志";
const SYNC_ALL_PARTIAL_1_MSG = "同步完成但有 1 条失败，详见后端日志";
const RESYNC_DONE_MSG = "KPI_SUPPLIER_OTD 已重新同步";
const RESYNC_NOT_PUBLISHED_MSG = "仅 PUBLISHED 状态的 KPI 建向量";
const RESYNC_FAILED_MSG = "重新同步失败，请稍后重试";

/** sync-missing 无缺失基线（后端 `KpiSyncMissingResult` 全字段）。 */
const NO_MISSING = {
  vectorPublishedTotal: 12,
  vectorMissingCount: 0,
  vectorSyncedCount: 0,
  vectorOrphanCount: 0,
  graphMissingCount: 0,
  graphSyncedCount: 0,
  failedCount: 0,
  failures: [] as Array<{ entityType?: string; error: string }>,
};

beforeEach(() => {
  useAuthStore.setState({ user: ADMIN_USER });
});

describe("KPI 目录页 admin 同步入口", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listKpis.mockResolvedValue([mockKpi]);
    datasourceApi.listDataSources.mockResolvedValue([
      { id: 7, name: "DWS_TEST", isActive: true },
    ]);
    api.getDatasourceSuggestions.mockResolvedValue([]);
    ontologyApi.listMetrics.mockResolvedValue([]);
  });

  it("admin 见页头「向量/图同步」按钮，点击调 syncMissingKpi 并提示无缺失", async () => {
    const user = userEvent.setup();
    const successSpy = vi
      .spyOn(message, "success")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.success>);
    api.syncMissingKpi.mockResolvedValue(NO_MISSING);
    renderPage();

    const btn = await screen.findByRole("button", { name: /向量\/图同步/ });
    await user.click(btn);

    await waitFor(() => {
      expect(api.syncMissingKpi).toHaveBeenCalledTimes(1);
      expect(successSpy).toHaveBeenCalledWith(SYNC_ALL_NONE_MSG);
    });
    successSpy.mockRestore();
  });

  it("同步有失败时用 warning 提示（不是 success）", async () => {
    const user = userEvent.setup();
    const warningSpy = vi
      .spyOn(message, "warning")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.warning>);
    const successSpy = vi
      .spyOn(message, "success")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.success>);
    api.syncMissingKpi.mockResolvedValue({
      ...NO_MISSING,
      vectorMissingCount: 1,
      vectorSyncedCount: 1,
      failedCount: 2,
      failures: [{ error: "boom" }],
    });
    renderPage();

    await user.click(await screen.findByRole("button", { name: /向量\/图同步/ }));

    await waitFor(() => {
      expect(warningSpy).toHaveBeenCalledWith(SYNC_ALL_PARTIAL_2_MSG);
    });
    expect(successSpy).not.toHaveBeenCalled();
    warningSpy.mockRestore();
    successSpy.mockRestore();
  });

  // 🔴 C11：**反向守卫** —— 上一条用例的 `vectorMissingCount: 1` 非 0 ⇒ 旧顺序（旧）也会落到 warning
  // ⇒ **抓不到**「计数全 0 但有失败」这一假绿。此条把三个计数**全置 0**、只留 `failedCount > 0`
  // （= 后端「降级读 ⇒ 跳过整段」的真实返回），断言**调 warning 且 success 未被调**。
  it("降级读（计数全 0 但 failedCount>0）走 warning，绝不报「无缺失」", async () => {
    const user = userEvent.setup();
    const warningSpy = vi
      .spyOn(message, "warning")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.warning>);
    const successSpy = vi
      .spyOn(message, "success")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.success>);
    api.syncMissingKpi.mockResolvedValue({
      ...NO_MISSING, // 三个计数全 0
      failedCount: 1,
      failures: [{ entityType: "milvus", error: "milvus 读失败" }],
    });
    renderPage();

    await user.click(await screen.findByRole("button", { name: /向量\/图同步/ }));

    await waitFor(() => {
      expect(warningSpy).toHaveBeenCalledWith(SYNC_ALL_PARTIAL_1_MSG);
    });
    // 关键断言：**不得**出现「无缺失：向量与图均已是最新」
    expect(successSpy).not.toHaveBeenCalled();
    warningSpy.mockRestore();
    successSpy.mockRestore();
  });

  it("非 admin 不渲染页头同步按钮", async () => {
    useAuthStore.setState({ user: NORMAL_USER });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /向量\/图同步/ })).toBeNull();
  });

  it("行级「重新同步」点击调 resyncKpiEmbedding(id) 并提示成功文案", async () => {
    const user = userEvent.setup();
    const successSpy = vi
      .spyOn(message, "success")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.success>);
    api.resyncKpiEmbedding.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    const btns = await screen.findAllByRole("button", { name: /重新同步/ });
    await user.click(btns[0]);

    await waitFor(() => {
      expect(api.resyncKpiEmbedding).toHaveBeenCalledWith(1);
      expect(successSpy).toHaveBeenCalledWith(RESYNC_DONE_MSG);
    });
    successSpy.mockRestore();
  });

  it("非 PUBLISHED 的 409 走 resyncNotPublished 专用提示", async () => {
    const user = userEvent.setup();
    const errorSpy = vi
      .spyOn(message, "error")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    api.resyncKpiEmbedding.mockRejectedValue(
      Object.assign(new Error("conflict"), { status: 409 }),
    );
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    const btns = await screen.findAllByRole("button", { name: /重新同步/ });
    await user.click(btns[0]);

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(RESYNC_NOT_PUBLISHED_MSG);
    });
    // 两条 error 分支文案不同 ⇒ 钉住，防止删掉 409 判断仍绿
    expect(errorSpy).not.toHaveBeenCalledWith(RESYNC_FAILED_MSG);
    errorSpy.mockRestore();
  });

  it("非 409 拒绝走 resyncFailed 通用提示（与 409 文案不同）", async () => {
    const user = userEvent.setup();
    const errorSpy = vi
      .spyOn(message, "error")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    api.resyncKpiEmbedding.mockRejectedValue(
      Object.assign(new Error("boom"), { status: 500 }),
    );
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    const btns = await screen.findAllByRole("button", { name: /重新同步/ });
    await user.click(btns[0]);

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(RESYNC_FAILED_MSG);
    });
    expect(errorSpy).not.toHaveBeenCalledWith(RESYNC_NOT_PUBLISHED_MSG);
    expect(RESYNC_NOT_PUBLISHED_MSG).not.toBe(RESYNC_FAILED_MSG);
    errorSpy.mockRestore();
  });
});
