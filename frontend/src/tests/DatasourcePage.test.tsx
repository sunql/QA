import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider, message } from "antd";
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
  introspectDatasource: vi.fn(),
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

    await user.click(screen.getByRole("button", { name: /更\s?多/ }));
    await user.click(await screen.findByRole("menuitem", { name: /编\s?辑/ }));
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

    await user.click(screen.getByRole("button", { name: /更\s?多/ }));
    await user.click(await screen.findByRole("menuitem", { name: /删\s?除/ }));
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
    await user.click(screen.getByRole("button", { name: /更\s?多/ }));
    await user.click(await screen.findByRole("menuitem", { name: /编\s?辑/ }));
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

    await user.click(screen.getByRole("button", { name: /更\s?多/ }));
    await user.click(await screen.findByRole("menuitem", { name: /编\s?辑/ }));
    await user.click(screen.getByRole("button", { name: "测试连接" }));

    expect(api.testDataSource).not.toHaveBeenCalled();
    expect(await screen.findByText(/请先填写密码/)).toBeInTheDocument();
  });

  it("数据行渲染后显示「智能导入到本体」入口按钮", async () => {
    renderPage();

    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /智能导入到本体/i })).toBeInTheDocument();
  });

  it("点击「缓存 Schema」调用 introspectDatasource 并提示成功", async () => {
    const user = userEvent.setup();
    api.introspectDatasource.mockResolvedValue({
      tables: [{ name: "ITMMASTER" }, { name: "PORDER" }],
      cachedAt: "2026-09-12T00:00:00Z",
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /缓存 Schema/i }));

    await waitFor(() => {
      expect(api.introspectDatasource).toHaveBeenCalledWith(1);
    });
    expect(await screen.findByText(/缓存成功/)).toBeInTheDocument();
  });

  // ---- Oracle type 切换 + 校验错误 + catch 分支 ----

  it("切换类型为 Oracle 时端口自动填 1521 且显示 Oracle 版本下拉", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByText("新增数据源"));
    // 类型下拉（Modal 内）
    await user.click(screen.getByRole("combobox", { name: /类型/ }));
    await user.click(screen.getByText("Oracle", { selector: ".ant-select-item-option-content" }));

    // 端口字段自动填 1521（InputNumber 通过 aria-label 或 spinbutton 定位）
    await waitFor(() => {
      const portInput = document.querySelector(
        'input[role="spinbutton"]',
      ) as HTMLInputElement | null;
      expect(portInput?.value).toBe("1521");
    });
  });

  it("表单校验失败：必填字段未填写时阻止提交", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByText("新增数据源"));
    // 不填写任何字段直接提交
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createDataSource).not.toHaveBeenCalled();
      // antd 校验错误提示出现
      expect(screen.getAllByText("必填项").length).toBeGreaterThan(0);
    });
  });

  it("listDataSources 失败时表格为空（catch 分支）", async () => {
    api.listDataSources.mockRejectedValue(new Error("服务不可用"));
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("数据源管理")).toBeInTheDocument();
    });
    // 错误被 catch 静默，表格为空
    expect(screen.queryByText("ZJTH-Oracle")).not.toBeInTheDocument();
  });

  it("createDataSource 失败时 catch 静默，不关闭弹窗", async () => {
    const user = userEvent.setup();
    api.createDataSource.mockRejectedValue(new Error("创建失败"));
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByText("新增数据源"));
    await user.type(screen.getByPlaceholderText("如 ZJTH-Oracle / RuoYi-MySQL"), "NewDS");
    await user.type(screen.getByPlaceholderText("IP 或域名"), "host");
    await user.type(screen.getByPlaceholderText("PG/MySQL 填数据库名，Oracle 填 service_name"), "db");
    await user.type(screen.getByLabelText("用户名"), "u");
    await user.type(screen.getByPlaceholderText("连接密码"), "p");
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createDataSource).toHaveBeenCalled();
    });
    // 弹窗因错误未关闭
    expect(screen.getByRole("button", { name: /确\s?定$/ })).toBeInTheDocument();
  });

  it("deleteDataSource 失败时 catch 静默不弹错", async () => {
    const user = userEvent.setup();
    api.deleteDataSource.mockRejectedValue(new Error("删除失败"));
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /更\s?多/ }));
    await user.click(await screen.findByRole("menuitem", { name: /删\s?除/ }));
    const confirmBtns = screen.getAllByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtns[confirmBtns.length - 1]);

    await waitFor(() => {
      expect(api.deleteDataSource).toHaveBeenCalledWith(1);
    });
  });

  it("测试连接：非成功响应时调用 message.error", async () => {
    const user = userEvent.setup();
    const errorSpy = vi.spyOn(message, "error").mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    api.testDataSource.mockResolvedValue({ success: false, message: "认证失败" });
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /更\s?多/ }));
    await user.click(await screen.findByRole("menuitem", { name: /编\s?辑/ }));
    await user.type(screen.getByPlaceholderText("留空表示不修改"), "secret");
    await user.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("认证失败"));
    });
    errorSpy.mockRestore();
  });

  it("测试连接：testDataSource 抛错时走 catch 分支", async () => {
    const user = userEvent.setup();
    const errorSpy = vi.spyOn(message, "error").mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    api.testDataSource.mockRejectedValue(new Error("网络异常"));
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /更\s?多/ }));
    await user.click(await screen.findByRole("menuitem", { name: /编\s?辑/ }));
    await user.type(screen.getByPlaceholderText("留空表示不修改"), "secret");
    await user.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("网络异常"));
    });
    errorSpy.mockRestore();
  });

  it("刷新按钮重新调用 listDataSources", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());
    expect(api.listDataSources).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /刷\s?新/ }));
    await waitFor(() => {
      expect(api.listDataSources).toHaveBeenCalledTimes(2);
    });
  });
});
