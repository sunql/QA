/** 待到货明细页：筛选 → 服务端分页 → 表格渲染。
 *
 * 判据要点：
 * - 初次加载带默认参数（近 12 个月）+ 分页 limit；
 * - 筛选变化 ⇒ offset 重置为 0（回到第 1 页）；
 * - 表格行渲染供应商名与物料描述（后端已带出）；
 * - 数据源选择仅 admin 可见。
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n/i18n";
import ProcurementPendingDeliveryPage from "../pages/ProcurementPendingDeliveryPage";

const api = vi.hoisted(() => ({
  listPendingDeliveryLines: vi.fn(),
  listDataSources: vi.fn(),
  listPendingDeliverySites: vi.fn(),
  searchMappings: vi.fn(),
}));

vi.mock("../api/pendingDelivery", () => ({
  listPendingDeliveryLines: api.listPendingDeliveryLines,
}));
vi.mock("../api/datasource", () => ({ listDataSources: api.listDataSources }));
vi.mock("../api/procurementCopilot", () => ({
  listPendingDeliverySites: api.listPendingDeliverySites,
}));
vi.mock("../api/entityMapping", () => ({ searchMappings: api.searchMappings }));

const authState = vi.hoisted(() => ({ roles: ["admin"] as string[] }));
vi.mock("../stores/authStore", () => ({
  useAuthStore: (selector: (state: { user: unknown }) => unknown) =>
    selector({ user: { roles: authState.roles } }),
}));

function renderPage() {
  return render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <ConfigProvider locale={zhCN}>
          <ProcurementPendingDeliveryPage />
        </ConfigProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  authState.roles = ["admin"];
  api.listDataSources.mockResolvedValue([
    { id: 1, name: "THBI Oracle", type: "oracle", isDefault: true },
  ]);
  api.listPendingDeliverySites.mockResolvedValue([
    { code: "T3", name: "模具厂" },
  ]);
  api.searchMappings.mockResolvedValue([]);
  api.listPendingDeliveryLines.mockResolvedValue({
    total: 42,
    offset: 0,
    limit: 20,
    rows: [
      {
        poNo: "T22511POH1251Q",
        poLine: 1000,
        materialCode: "502447700001",
        materialDesc: "X13 平头铆螺母",
        supplierCode: "F109",
        supplierName: "上海纳特汽车标准件有限公司",
        orderedQty: 1000000,
        receivedQty: 9600,
        openQty: 990400,
        expectedDate: "2025-11-28 00:00:00",
        overdueDays: 360,
      },
    ],
  });
});

describe("ProcurementPendingDeliveryPage", () => {
  it("初次加载：带默认时间窗 + 分页参数，行显示供应商名与物料描述", async () => {
    renderPage();
    await waitFor(() =>
      expect(api.listPendingDeliveryLines).toHaveBeenCalled(),
    );

    const query = api.listPendingDeliveryLines.mock.calls[0][0];
    expect(query.monthsBack).toBe(12);
    expect(query.limit).toBe(20);
    expect(query.offset).toBe(0);
    // 数据源在 listDataSources 回来后才有值 ⇒ 等到带 datasourceId 的那次请求
    await waitFor(() =>
      expect(
        api.listPendingDeliveryLines.mock.calls.some(
          (call) => call[0]?.datasourceId === 1,
        ),
      ).toBe(true),
    );

    expect(screen.getByText("T22511POH1251Q")).toBeTruthy();
    expect(screen.getByText("X13 平头铆螺母")).toBeTruthy();
    expect(screen.getByText("上海纳特汽车标准件有限公司")).toBeTruthy();
    expect(screen.getByText("990,400")).toBeTruthy();
  });

  it("改筛选（最低逾期天数）⇒ offset 重置为 0 并重新请求", async () => {
    renderPage();
    await waitFor(() =>
      expect(api.listPendingDeliveryLines).toHaveBeenCalled(),
    );
    const firstCalls = api.listPendingDeliveryLines.mock.calls.length;

    // 两个 InputNumber：月份、逾期天数（后者在后）
    const overdueInput = screen.getAllByRole("spinbutton")[1];
    fireEvent.change(overdueInput, { target: { value: "30" } });

    await waitFor(() =>
      expect(api.listPendingDeliveryLines.mock.calls.length).toBeGreaterThan(
        firstCalls,
      ),
    );
    const calls = api.listPendingDeliveryLines.mock.calls;
    const last = calls[calls.length - 1][0];
    expect(last.minOverdueDays).toBe(30);
    expect(last.offset).toBe(0);
  });

  it("点「查询」重新拉数；点「重置」清空筛选并回第 1 页", async () => {
    renderPage();
    await waitFor(() =>
      expect(api.listPendingDeliveryLines).toHaveBeenCalled(),
    );
    const before = api.listPendingDeliveryLines.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: /查询/ }));
    await waitFor(() =>
      expect(api.listPendingDeliveryLines.mock.calls.length).toBeGreaterThan(
        before,
      ),
    );

    // 重置：清筛选 + 回第 1 页（offset=0）
    fireEvent.click(screen.getByRole("button", { name: /重置/ }));
    await waitFor(() => {
      const calls = api.listPendingDeliveryLines.mock.calls;
      const last = calls[calls.length - 1][0];
      expect(last?.offset).toBe(0);
      expect(last?.monthsBack).toBe(12);
      expect(last?.supplierKeys ?? []).toEqual([]);
    });
  });

  it("搜索供应商 ⇒ 走 entity-mapping 远程搜索", async () => {
    api.searchMappings.mockResolvedValue([
      {
        id: 1,
        entityType: "SUPPLIER",
        enterpriseKey: 1,
        enterpriseCode: "F109",
        sourceSystem: "ERP",
        sourceCode: "ERP-1",
        name: "上海纳特",
      },
    ]);
    renderPage();
    await waitFor(() =>
      expect(api.listPendingDeliveryLines).toHaveBeenCalled(),
    );

    // aria-label 同时落在 combobox 与其内部 input ⇒ 取 combobox 那个
    const supplierSelect = screen
      .getAllByLabelText("搜索供应商名称/编码")
      .find((el) => el.getAttribute("role") === "combobox") as HTMLElement;
    fireEvent.mouseDown(supplierSelect);
    fireEvent.change(supplierSelect, { target: { value: "纳特" } });
    await waitFor(() => expect(api.searchMappings).toHaveBeenCalled());
    expect(api.searchMappings.mock.calls[0][1]).toMatchObject({
      entityType: "SUPPLIER",
    });
  });

  it("非 admin：看不到数据源选择", async () => {
    authState.roles = ["procurement"];
    renderPage();
    await waitFor(() =>
      expect(api.listPendingDeliveryLines).toHaveBeenCalled(),
    );
    expect(screen.queryByText("数据源")).toBeNull();
  });
});
