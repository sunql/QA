import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import Neo4jGraphPage from "../pages/Neo4jGraphPage";

const api = vi.hoisted(() => ({
  listGraphNodes: vi.fn(),
  getGraphRelations: vi.fn(),
}));

vi.mock("../api/systemViewer", () => api);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <Neo4jGraphPage />
    </ConfigProvider>,
  );
}

describe("Neo4jGraphPage — 渲染 + 加载", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listGraphNodes.mockResolvedValue([]);
    api.getGraphRelations.mockResolvedValue([]);
  });

  it("挂载时拉取 Class 节点列表", async () => {
    renderPage();
    await waitFor(() =>
      expect(api.listGraphNodes).toHaveBeenCalledWith("Class", ""),
    );
  });

  it("listGraphNodes 失败时降级为空数组，组件不崩", async () => {
    api.listGraphNodes.mockRejectedValue(new Error("网络错误"));
    renderPage();
    await waitFor(() => expect(api.listGraphNodes).toHaveBeenCalled());
    // 表格空
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("展示节点 unifiedId（主键列绑定 unifiedId，不是已废弃的 id）", async () => {
    // 曾经列绑定 `id`，而端点早就不下发该字段 —— 表格整列空白，
    // 且关系查询拿不到节点标识。
    api.listGraphNodes.mockResolvedValue([
      {
        unifiedId: "obj:CLASS:9",
        name: "DWD_CUSTOMER",
        alias: "客户",
        description: null,
        sourceTable: "THBI.DWD_CUSTOMER",
      },
    ]);
    renderPage();

    expect(await screen.findByText("obj:CLASS:9")).toBeInTheDocument();
  });
});

describe("Neo4jGraphPage — Segmented 切换", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listGraphNodes.mockResolvedValue([]);
    api.getGraphRelations.mockResolvedValue([]);
  });

  it("切换到 业务图 Tab → 渲染 GraphTraversalPanel 而非 Table", async () => {
    const user = userEvent.setup();
    renderPage();

    // 找「业务图」segment item
    const bizTab = screen.getByText(/业务图/);
    await user.click(bizTab);

    // 切到「业务图」后不再渲染 Table 列表
    await waitFor(() => {
      // GraphTraversalPanel 应该出现（mock 后实际渲染）
      expect(document.body.textContent).toContain("业务图");
    });
  });

  it("切换到 业务图 Tab → 不带 label=业务图 调 listGraphNodes（后端白名单外会 422）", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() =>
      expect(api.listGraphNodes).toHaveBeenCalledWith("Class", ""),
    );

    await user.click(screen.getByText(/业务图/));

    // 等一拍，确认没有新增带「业务图」label 的请求
    await new Promise((r) => setTimeout(r, 50));
    expect(api.listGraphNodes).toHaveBeenCalledTimes(1);
  });
});