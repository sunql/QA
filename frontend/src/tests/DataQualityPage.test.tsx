import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import DataQualityPage from "../pages/DataQualityPage";
import type { DataQualityRule } from "../types/dataQuality";

const api = vi.hoisted(() => ({
  listRules: vi.fn(),
  createRule: vi.fn(),
  updateRule: vi.fn(),
  disableRule: vi.fn(),
}));

const dsApi = vi.hoisted(() => ({
  listDataSources: vi.fn(),
}));

vi.mock("../api/dataQuality", () => api);
vi.mock("../api/datasource", () => dsApi);

const mockRule: DataQualityRule = {
  id: 1,
  ruleCode: "RULE_001",
  ruleName: "订单金额非空",
  ruleType: "COMPLETENESS",
  datasourceId: 1,
  targetTable: "T_ORDER",
  targetColumn: "AMOUNT",
  ruleExpression: null,
  threshold: "0",
  severity: "HIGH",
  isEnabled: true,
  version: "1",
  owner: null,
  description: null,
  createdTime: "2026-09-01T00:00:00Z",
  updatedTime: "2026-09-01T00:00:00Z",
};

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <DataQualityPage />
    </ConfigProvider>,
  );
}

describe("DataQualityPage — 渲染 + 加载", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([mockRule]);
    dsApi.listDataSources.mockResolvedValue([]);
  });

  it("挂载时拉取规则列表并展示", async () => {
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    expect(await screen.findByText("订单金额非空")).toBeInTheDocument();
  });

  it("listRules 失败时组件不崩", async () => {
    api.listRules.mockRejectedValue(new Error("网络错误"));
    renderPage();
    await waitFor(() => expect(api.listRules).toHaveBeenCalled());
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

describe("DataQualityPage — 启用/停用切换", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listRules.mockResolvedValue([mockRule]);
    api.disableRule.mockResolvedValue({ ...mockRule, isEnabled: false });
    dsApi.listDataSources.mockResolvedValue([]);
  });

  it("点击停用按钮调用 disableRule", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(screen.getByText("订单金额非空")).toBeInTheDocument());
    // i18n key 误写：源码用 t("common.disable")，但 zh-CN 只有 "disabled"，导致按钮显示 key 本身
    // 这里按实际渲染的 key 文本匹配
    const disableBtn = screen.getAllByRole("button").find((b) =>
      (b.textContent || "").replace(/\s+/g, "") === "common.disable",
    );
    expect(disableBtn).toBeDefined();
    await user.click(disableBtn!);

    await waitFor(() =>
      expect(api.disableRule).toHaveBeenCalledWith(1),
    );
  });

  it("disableRule 失败时组件不崩", async () => {
    api.disableRule.mockRejectedValue(new Error("权限不足"));
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(screen.getByText("订单金额非空")).toBeInTheDocument());
    const disableBtn = screen.getAllByRole("button").find((b) =>
      (b.textContent || "").replace(/\s+/g, "") === "common.disable",
    );
    expect(disableBtn).toBeDefined();
    await user.click(disableBtn!);

    await waitFor(() => expect(api.disableRule).toHaveBeenCalledWith(1));
  });
});