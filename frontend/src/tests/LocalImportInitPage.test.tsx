import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import LocalImportInitPage from "../pages/LocalImportInitPage";
import type { DataSource, SchemaIntrospectResponse } from "../types/datasource";

// =============================================================================
// Mocks
// =============================================================================

vi.mock("../api/localImport");

const api = vi.hoisted(() => ({
  listDataSources: vi.fn(),
  getDatasourceSchema: vi.fn(),
  introspectDatasource: vi.fn(),
}));

vi.mock("../api/datasource", () => api);

function ds(id: number, name: string, overrides: Partial<DataSource> = {}): DataSource {
  return {
    id,
    name,
    type: "oracle",
    host: "localhost",
    port: 1521,
    databaseName: name,
    username: "u",
    description: null,
    isActive: true,
    isDefault: false,
    oracleVersion: "12c",
    createdBy: null,
    createdTime: "2026-01-01T00:00:00Z",
    updatedTime: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function schemaResponse(): SchemaIntrospectResponse {
  return { tables: [], cachedAt: "2026-01-01T00:00:00Z" };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getDatasourceSchema.mockResolvedValue(schemaResponse());
  api.introspectDatasource.mockResolvedValue(schemaResponse());
});

// =============================================================================
// 数据源选择 + 打开向导
// =============================================================================

describe("LocalImportInitPage", () => {
  it("默认选中 isDefault 数据源；点「开始导入」打开向导并请求该 id 的 schema", async () => {
    api.listDataSources.mockResolvedValue([
      ds(2, "Staging Oracle"),
      ds(1, "THBI Oracle", { isDefault: true }),
    ]);
    render(<LocalImportInitPage />);

    // Select 默认值显示 isDefault 项（即使不是列表第一项）
    expect(await screen.findByText("THBI Oracle")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /开始导入/i }));

    // 向导打开：出现「选择要导入的表」（wizard-only 文案）
    expect(await screen.findByText(/选择要导入的表/i)).toBeInTheDocument();
    await waitFor(() => expect(api.getDatasourceSchema).toHaveBeenCalledWith(1));
  });

  it("无 isDefault 数据源时默认选中列表第一个", async () => {
    api.listDataSources.mockResolvedValue([ds(3, "Alpha"), ds(4, "Beta")]);
    render(<LocalImportInitPage />);

    expect(await screen.findByText("Alpha")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /开始导入/i }));

    await waitFor(() => expect(api.getDatasourceSchema).toHaveBeenCalledWith(3));
  });

  it("加载失败时显示错误，不出现开始按钮与向导", async () => {
    api.listDataSources.mockRejectedValue(new Error("网络错误"));
    render(<LocalImportInitPage />);

    expect(await screen.findByText(/加载失败/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /开始导入/i })).not.toBeInTheDocument();
  });

  it("无数据源时显示空状态提示", async () => {
    api.listDataSources.mockResolvedValue([]);
    render(<LocalImportInitPage />);

    expect(await screen.findByText(/暂无可用数据源/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /开始导入/i })).not.toBeInTheDocument();
  });

  it("关闭向导后 onClose 复位；再次点击可重新打开", async () => {
    api.listDataSources.mockResolvedValue([ds(1, "THBI Oracle", { isDefault: true })]);
    render(<LocalImportInitPage />);

    await screen.findByText("THBI Oracle");
    fireEvent.click(screen.getByRole("button", { name: /开始导入/i }));
    expect(await screen.findByText(/选择要导入的表/i)).toBeInTheDocument();

    // antd Modal 默认右上角关闭按钮
    const closeBtn = document.querySelector(".ant-modal-close") as HTMLElement | null;
    expect(closeBtn).not.toBeNull();
    fireEvent.click(closeBtn!);
    await waitFor(() =>
      expect(screen.queryByText(/选择要导入的表/i)).not.toBeInTheDocument(),
    );

    // 复位后可再次打开（重新触发 schema 拉取）
    api.getDatasourceSchema.mockClear();
    fireEvent.click(screen.getByRole("button", { name: /开始导入/i }));
    await waitFor(() => expect(api.getDatasourceSchema).toHaveBeenCalledWith(1));
  });
});
