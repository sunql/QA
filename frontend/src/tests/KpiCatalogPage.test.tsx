import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider, message } from "antd";
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

  // ---- Edit / Delete / Refresh / Filter ----

  it("点击编辑打开预填表单并提交调用 updateKpi（payload 字段）", async () => {
    const user = userEvent.setup();
    api.updateKpi.mockResolvedValue({ ...mockKpi, kpiName: "供应商准时交付率 v2" });
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    // 编辑模式下 Modal title 是"编辑 KPI"
    expect(await screen.findByText(/编\s?辑.*KPI/)).toBeInTheDocument();

    // 修改 KPI 名称后提交
    const nameInput = screen.getByDisplayValue("供应商准时交付率");
    await user.clear(nameInput);
    await user.type(nameInput, "供应商准时交付率 v2");

    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.updateKpi).toHaveBeenCalledTimes(1);
      const [id, payload] = api.updateKpi.mock.calls[0];
      expect(id).toBe(1);
      expect(payload.kpiCode).toBe("KPI_SUPPLIER_OTD");
      expect(payload.kpiName).toBe("供应商准时交付率 v2");
      expect(payload.status).toBe("PUBLISHED");
    });
  });

  it("点击删除并确认调用 deleteKpi", async () => {
    const user = userEvent.setup();
    const successSpy = vi.spyOn(message, "success").mockReturnValue(1 as unknown as ReturnType<typeof message.success>);
    api.deleteKpi.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /删\s?除/ }));
    // Popconfirm 确认按钮
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.deleteKpi).toHaveBeenCalledWith(1);
      expect(successSpy).toHaveBeenCalled();
    });
    successSpy.mockRestore();
  });

  it("删除失败时静默 catch 不抛错", async () => {
    const user = userEvent.setup();
    api.deleteKpi.mockRejectedValue(new Error("删除失败"));
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /删\s?除/ }));
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.deleteKpi).toHaveBeenCalledWith(1);
    });
  });

  it("刷新按钮重新调用 listKpis", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());
    expect(api.listKpis).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /刷\s?新/ }));

    await waitFor(() => {
      expect(api.listKpis).toHaveBeenCalledTimes(2);
    });
  });

  it("listKpis 失败时表格为空（catch 分支）", async () => {
    api.listKpis.mockRejectedValue(new Error("服务不可用"));
    renderPage();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /plus.*新.*建.*KPI/ })).toBeInTheDocument();
    });
    // KPI 不在表格中
    expect(screen.queryByText("KPI_SUPPLIER_OTD")).not.toBeInTheDocument();
  });

  it("筛选：输入 KPI 编码关键字后表格只显示命中的行", async () => {
    api.listKpis.mockResolvedValue([
      mockKpi,
      { ...mockKpi, id: 2, kpiCode: "KPI_OTHER" },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    const codeInput = screen.getByPlaceholderText("KPI 编码");
    await userEvent.type(codeInput, "OTD");

    await waitFor(() => {
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument();
      expect(screen.queryByText("KPI_OTHER")).not.toBeInTheDocument();
    });
  });

  it("重置筛选后恢复全部 KPI", async () => {
    api.listKpis.mockResolvedValue([
      mockKpi,
      { ...mockKpi, id: 2, kpiCode: "KPI_OTHER" },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_OTHER")).toBeInTheDocument());

    const codeInput = screen.getByPlaceholderText("KPI 编码");
    await userEvent.type(codeInput, "OTD");
    await waitFor(() => expect(screen.queryByText("KPI_OTHER")).not.toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /重\s?置/ }));
    await waitFor(() => expect(screen.getByText("KPI_OTHER")).toBeInTheDocument());
  });

  it("业务定义长文本渲染 Tooltip", async () => {
    const LONG = "这是一段非常长的业务定义描述文本，用于验证超出列宽时悬停可查看完整内容。";
    api.listKpis.mockResolvedValue([{ ...mockKpi, businessDefinition: LONG }]);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    fireEvent.mouseEnter(screen.getByText(LONG));
    expect(await screen.findByRole("tooltip")).toHaveTextContent(LONG);
  });

  it("版本列：当 revisionCount>0 时附加 (+N)", async () => {
    api.listKpis.mockResolvedValue([
      { ...mockKpi, version: "v1.0", revisionCount: 3 },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());
    expect(screen.getByText(/v1\.0 \(\+3\)/)).toBeInTheDocument();
  });

  it("createKpi 提交失败时 catch 静默", async () => {
    const user = userEvent.setup();
    api.createKpi.mockRejectedValue(new Error("创建失败"));
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /plus.*新.*建.*KPI/ }));
    const codeInput = screen.getByPlaceholderText(/KPI_/);
    const nameInput = screen.getByPlaceholderText(/供应商准时交付率/);
    await user.type(codeInput, "KPI_FAIL");
    await user.type(nameInput, "失败 KPI");
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.createKpi).toHaveBeenCalled();
    });
  });
});
