import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import DatasourcePage from "../pages/DatasourcePage";
import type { DataSource } from "../types/datasource";

// vi.mock 工厂会被提升到顶部，用 vi.hoisted 保证 mockDataSource 先初始化
const { mockDataSource } = vi.hoisted(() => ({
  mockDataSource: {
    id: 1,
    name: "ZJTH-Oracle",
    type: "oracle",
    host: "192.168.205.70",
    port: 1521,
    databaseName: "X3V71ORA",
    username: "ZJTH",
    description: "Sage X3 Oracle",
    isActive: true,
    isDefault: true,
    oracleVersion: "11g",
    createdBy: "system",
    createdTime: "2026-08-12T00:00:00Z",
    updatedTime: "2026-08-12T00:00:00Z",
  } as DataSource,
}));

const api = vi.hoisted(() => ({
  listDataSources: vi.fn(),
  createDataSource: vi.fn(),
  updateDataSource: vi.fn(),
  deleteDataSource: vi.fn(),
  testDataSource: vi.fn(),
}));

vi.mock("../api/datasource", () => api);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/datasource"]}>
        <Routes>
          <Route path="/datasource" element={<DatasourcePage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("DatasourcePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listDataSources.mockResolvedValue([mockDataSource]);
  });

  it("渲染标题与表格，并加载数据源列表", async () => {
    renderPage();

    expect(screen.getByText("数据源管理")).toBeInTheDocument();
    expect(screen.getByText("新增数据源")).toBeInTheDocument();

    // 等待异步数据加载后行渲染
    await waitFor(() => {
      expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument();
      expect(screen.getByText("192.168.205.70")).toBeInTheDocument();
      expect(screen.getByText("ORACLE")).toBeInTheDocument();
    });
  });

  it("点击新增并提交调用 createDataSource", async () => {
    const user = userEvent.setup();
    api.createDataSource.mockResolvedValue({ ...mockDataSource, id: 2 });
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByText("新增数据源"));
    await user.type(screen.getByPlaceholderText("如 ZJTH-Oracle / RuoYi-MySQL"), "RuoYi-MySQL");
    await user.type(screen.getByPlaceholderText("IP 或域名"), "db.example.com");
    await user.type(
      screen.getByPlaceholderText("PG/MySQL 填数据库名，Oracle 填 service_name"),
      "wms"
    );
    await user.type(screen.getByLabelText("用户名"), "root");
    await user.type(screen.getByPlaceholderText("连接密码"), "secret");

    // 模态框确定按钮（antd 对两字中文按钮自动加空格："确 定"）
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createDataSource).toHaveBeenCalledTimes(1);
      const payload = api.createDataSource.mock.calls[0][0];
      expect(payload.name).toBe("RuoYi-MySQL");
      expect(payload.type).toBe("postgresql");
      expect(payload.host).toBe("db.example.com");
      expect(payload.databaseName).toBe("wms");
      expect(payload.username).toBe("root");
      expect(payload.password).toBe("secret");
    });
  });

  it("点击编辑并提交调用 updateDataSource，且不传空密码", async () => {
    const user = userEvent.setup();
    api.updateDataSource.mockResolvedValue({ ...mockDataSource });
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    // 模态框打开，名称输入框预填了 ZJTH-Oracle
    expect(screen.getByDisplayValue("ZJTH-Oracle")).toBeInTheDocument();
    // 不填密码即提交（编辑时密码可选，留空表示不修改）
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.updateDataSource).toHaveBeenCalledTimes(1);
      const payload = api.updateDataSource.mock.calls[0][1];
      expect(payload.name).toBe("ZJTH-Oracle");
      expect(payload.password).toBeUndefined();
    });
  });

  it("点击删除并在确认后调用 deleteDataSource", async () => {
    const user = userEvent.setup();
    api.deleteDataSource.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /删\s?除/ }));
    const confirmBtns = screen.getAllByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtns[confirmBtns.length - 1]);

    await waitFor(() => {
      expect(api.deleteDataSource).toHaveBeenCalledWith(1);
    });
  });

  it("在弹窗内填写密码后测试连接，并显示结果", async () => {
    const user = userEvent.setup();
    api.testDataSource.mockResolvedValue({ success: true, message: "连接成功" });
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    // 打开编辑弹窗，填写密码后点测试连接
    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    await user.type(screen.getByPlaceholderText("留空表示不修改"), "secret");
    await user.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => {
      expect(api.testDataSource).toHaveBeenCalledTimes(1);
      const req = api.testDataSource.mock.calls[0][0];
      expect(req.type).toBe("oracle");
      expect(req.host).toBe("192.168.205.70");
      expect(req.port).toBe(1521);
      expect(req.databaseName).toBe("X3V71ORA");
      expect(req.username).toBe("ZJTH");
      expect(req.password).toBe("secret");
    });
    expect(await screen.findByText(/连接成功/)).toBeInTheDocument();
  });

  it("未填密码时点测试连接给出提示，不调用接口", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    await user.click(screen.getByRole("button", { name: "测试连接" }));

    expect(api.testDataSource).not.toHaveBeenCalled();
    expect(await screen.findByText(/请先填写密码/)).toBeInTheDocument();
  });

  it("数据行渲染后显示「智能导入到本体」入口按钮", async () => {
    renderPage();

    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /智能导入到本体/i })).toBeInTheDocument();
  });
});
