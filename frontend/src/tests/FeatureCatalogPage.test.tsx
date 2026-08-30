import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import FeatureCatalogPage from "../pages/FeatureCatalogPage";
import type { FeatureDefinition } from "../types/feature";

const { mockFeature } = vi.hoisted(() => ({
  mockFeature: {
    id: 1,
    featureName: "SUPPLIER_OTD_3M",
    featureAlias: "供应商3月准时交付率",
    featureDefinition: "供应商最近 3 个月准时交付率均值",
    entityType: "SUPPLIER",
    calculationLogic:
      "SELECT supplier_code AS entity_key, AVG(on_time_rate) AS value FROM DWS GROUP BY supplier_code",
    windowSize: "3M",
    refreshFrequency: "DAILY",
    unit: "%",
    owner: "procurement",
    version: "v1.0",
    status: "ACTIVE",
    isEnabled: true,
    datasourceId: 4,
    createdBy: null,
    createdTime: "2026-08-30T00:00:00Z",
    updatedTime: "2026-08-30T00:00:00Z",
  } as FeatureDefinition,
}));

const api = vi.hoisted(() => ({
  listFeatures: vi.fn(),
  createFeature: vi.fn(),
  updateFeature: vi.fn(),
  deleteFeature: vi.fn(),
  computeFeature: vi.fn(),
  computeAllFeatures: vi.fn(),
  listFeatureValues: vi.fn(),
}));

const dsApi = vi.hoisted(() => ({
  listDataSources: vi.fn(),
}));

vi.mock("../api/feature", () => api);
vi.mock("../api/datasource", () => dsApi);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/features"]}>
        <Routes>
          <Route path="/features" element={<FeatureCatalogPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("FeatureCatalogPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listFeatures.mockResolvedValue([mockFeature]);
    dsApi.listDataSources.mockResolvedValue([
      { id: 4, name: "THBI", type: "oracle" },
    ]);
  });

  it("渲染特征列表（名称/别名/状态 Tag）", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("SUPPLIER_OTD_3M")).toBeInTheDocument();
      expect(screen.getByText("供应商3月准时交付率")).toBeInTheDocument();
      // enums.featureStatus.ACTIVE = 已启用
      expect(screen.getByText("已启用")).toBeInTheDocument();
    });
  });

  it("渲染操作列按钮（特征值/计算/编辑/删除）", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("特征值")).toBeInTheDocument();
      expect(screen.getByText("计算")).toBeInTheDocument();
      expect(screen.getByText(/编\s?辑/)).toBeInTheDocument();
      expect(screen.getByText(/删\s?除/)).toBeInTheDocument();
    });
  });

  it("点击新建并提交调用 createFeature（camelCase payload，无 owner）", async () => {
    const user = userEvent.setup();
    api.createFeature.mockResolvedValue({ ...mockFeature, id: 2 });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("SUPPLIER_OTD_3M")).toBeInTheDocument(),
    );

    await user.click(
      screen.getByRole("button", { name: /新.*建.*特.*征/ }),
    );

    // 必填：featureName / calculationLogic / datasourceId（entityType 默认 SUPPLIER）
    const nameInput = screen.getByPlaceholderText(/SUPPLIER_OTD_3M/);
    await user.type(nameInput, "SUPPLIER_DEFECT_RATE_3M");

    const logicInput = screen.getByPlaceholderText(/supplier_code AS entity_key/);
    await user.type(logicInput, "SELECT 1 AS entity_key, 1 AS value FROM DWS");

    const dsSelect = screen.getByText(/选择业务数据源/).closest(".ant-select");
    if (!dsSelect) throw new Error("datasource select not found");
    fireEvent.mouseDown(dsSelect.querySelector(".ant-select-selector")!);
    await user.click(await screen.findByText("THBI (oracle)"));

    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.createFeature).toHaveBeenCalled();
      const payload = api.createFeature.mock.calls[0][0];
      expect(payload.featureName).toBe("SUPPLIER_DEFECT_RATE_3M");
      expect(payload.entityType).toBe("SUPPLIER");
      expect(payload.status).toBe("DRAFT");
      expect(payload.isEnabled).toBe(true);
      expect(payload.datasourceId).toBe(4);
      // owner / createdBy 由服务端派生，前端 payload 不携带
      expect(payload.owner).toBeUndefined();
      expect(payload.createdBy).toBeUndefined();
    });
  });

  it("点击计算调用 computeFeature 并提示行数", async () => {
    const user = userEvent.setup();
    api.computeFeature.mockResolvedValue({ featureId: 1, rows: 2 });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("SUPPLIER_OTD_3M")).toBeInTheDocument(),
    );

    await user.click(screen.getByText("计算"));

    await waitFor(() => {
      expect(api.computeFeature).toHaveBeenCalledWith(1);
    });
  });
});
