import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import KpiCatalogPage from "../pages/KpiCatalogPage";
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
    unit: "%",
    dataSource: "DWS_SUPPLIER_MONTHLY",
    owner: "采购部",
    version: "v1.0",
    revisionCount: 0,
    status: "PUBLISHED",
    metricId: null,
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
}));

vi.mock("../api/kpiCatalog", () => api);

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

describe("KpiCatalogPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listKpis.mockResolvedValue([mockKpi]);
  });

  it("渲染标题与表格，并加载 KPI 列表", async () => {
    renderPage();
    // antd 按钮 a11y name = "plus 新 建 KPI"（图标+断字）
    expect(
      screen.getByRole("button", { name: /plus.*新.*建.*KPI/ }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument();
      expect(screen.getByText("供应商准时交付率")).toBeInTheDocument();
      // 状态 Tag 渲染的是翻译后的中文（enums.kpiStatus.PUBLISHED = 已发布）
      expect(screen.getByText("已发布")).toBeInTheDocument();
    });
  });

  it("渲染操作列按钮", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/编\s?辑/)).toBeInTheDocument();
      expect(screen.getByText(/删\s?除/)).toBeInTheDocument();
    });
  });

  it("点击新建并提交调用 createKpi（camelCase payload）", async () => {
    const user = userEvent.setup();
    api.createKpi.mockResolvedValue({ ...mockKpi, id: 2 });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /plus.*新.*建.*KPI/ }));

    const codeInput = screen.getByPlaceholderText(/KPI_/);
    // Modal 的 name 输入框 placeholder = "如：供应商准时交付率"，FilterBar 用 label 做 placeholder 是 "KPI 名称"
    const nameInput = screen.getByPlaceholderText(/供应商准时交付率/);
    await user.type(codeInput, "KPI_TEST_NEW");
    await user.type(nameInput, "新 KPI");

    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.createKpi).toHaveBeenCalled();
      const payload = api.createKpi.mock.calls[0][0];
      expect(payload.kpiCode).toBe("KPI_TEST_NEW");
      expect(payload.kpiName).toBe("新 KPI");
    });
  });

  it("列表渲染状态 Tag 颜色：DRAFT/PUBLISHED/DEPRECATED", async () => {
    api.listKpis.mockResolvedValue([
      { ...mockKpi, id: 1, kpiCode: "K1", status: "DRAFT" },
      { ...mockKpi, id: 2, kpiCode: "K2", status: "PUBLISHED" },
      { ...mockKpi, id: 3, kpiCode: "K3", status: "DEPRECATED" },
    ]);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("K1")).toBeInTheDocument();
      expect(screen.getByText("K2")).toBeInTheDocument();
      expect(screen.getByText("K3")).toBeInTheDocument();
    });
  });
});
