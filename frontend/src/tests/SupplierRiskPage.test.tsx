import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import SupplierRiskPage from "../pages/SupplierRiskPage";

const supplierRiskApi = vi.hoisted(() => ({
  getSupplierRisk: vi.fn(),
}));

const entityMappingApi = vi.hoisted(() => ({
  searchMappings: vi.fn(),
}));

vi.mock("../api/supplierRisk", () => supplierRiskApi);
vi.mock("../api/entityMapping", () => entityMappingApi);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <SupplierRiskPage />
    </ConfigProvider>,
  );
}

describe("SupplierRiskPage — 渲染", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    entityMappingApi.searchMappings.mockResolvedValue([]);
  });

  it("渲染标题 + AutoComplete 输入框", () => {
    renderPage();
    expect(screen.getByText(/供应商风险 Agent 直接查询/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/enterprise_key/)).toBeInTheDocument();
  });
});

describe("SupplierRiskPage — handleQuery 校验分支", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    entityMappingApi.searchMappings.mockResolvedValue([]);
  });

  it("supplierKey 为 null 时点击查询按钮 → 不调用 getSupplierRisk + 显示 Alert", async () => {
    const user = userEvent.setup();
    renderPage();

    // antd 在 Typography.Space 内会插入空格做间距：按钮 textContent 可能是 "查 询"
    const buttons = screen.getAllByRole("button");
    const queryBtn = buttons.find((b) =>
      (b.textContent || "").replace(/\s+/g, "") === "查询",
    );
    expect(queryBtn).toBeDefined();
    await user.click(queryBtn!);

    await waitFor(() =>
      expect(supplierRiskApi.getSupplierRisk).not.toHaveBeenCalled(),
    );
    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
  });
});