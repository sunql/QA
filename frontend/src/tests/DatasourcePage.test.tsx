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
  startIntrospectJob: vi.fn(),
  getIntrospectJob: vi.fn(),
}));

vi.mock("../api/datasource", () => api);

// 与 DatasourcePage 的 POLL_INTERVAL_MS 对齐：等 2 个轮询周期（1.5s × 2）再加余量。
const TWO_POLL_CYCLES_MS = 3_500;

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
    api.startIntrospectJob.mockReset();
    api.getIntrospectJob.mockReset();
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

  it("点击「缓存 Schema」启动内省任务并提示成功", async () => {
    const user = userEvent.setup();
    api.startIntrospectJob.mockResolvedValue({
      jobId: "j0",
      status: "running",
      total: 2,
      completed: 0,
    });
    api.getIntrospectJob.mockResolvedValue({
      jobId: "j0",
      status: "succeeded",
      total: 2,
      completed: 2,
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("ZJTH-Oracle")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /缓存 Schema/i }));

    await waitFor(() => {
      expect(api.startIntrospectJob).toHaveBeenCalledWith(1);
    });
    expect(await screen.findByText(/缓存成功/)).toBeInTheDocument();
  });

  // ---- 缓存 Schema 进度弹窗（异步 job + 轮询） ----

  it("缓存 Schema：打开进度弹窗并按轮询推进", async () => {
    const user = userEvent.setup();
    api.startIntrospectJob.mockResolvedValue({
      jobId: "j1", status: "running", total: 200, completed: 0,
    });
    api.getIntrospectJob
      .mockResolvedValueOnce({ jobId: "j1", status: "running", total: 200, completed: 100 })
      .mockResolvedValueOnce({ jobId: "j1", status: "succeeded", total: 200, completed: 200 });

    renderPage();
    await user.click(await screen.findByRole("button", { name: /缓存 Schema/i }));

    expect(api.startIntrospectJob).toHaveBeenCalled();
    expect(await screen.findByText(/正在缓存 schema/)).toBeTruthy();
    // 轮询首次返回 {completed:100,total:200} ⇒ 页面必须显示真实进度数值 100/200
    // （只断言“正在缓存 schema”证明不了进度被渲染）。
    expect(await screen.findByText(/100\/200/)).toBeInTheDocument();
    // 成功后自动关闭 + 成功提示（轮询间隔 1.5s，真实计时器下需放宽 waitFor 超时）。
    // antd Modal 关闭后仍保留（隐藏的）DOM，故断言「不可见 / 已移除」而非 queryByText 为 null。
    await waitFor(
      () => {
        const progress = screen.queryByText(/正在缓存 schema/);
        if (progress) expect(progress).not.toBeVisible();
      },
      { timeout: 12000 },
    );
    expect(await screen.findByText(/缓存成功/)).toBeInTheDocument();
  });

  it("缓存 Schema 失败：弹窗保持可见且停止轮询（不自动关闭）", async () => {
    const user = userEvent.setup();
    api.startIntrospectJob.mockResolvedValue({
      jobId: "j2", status: "running", total: 10, completed: 0,
    });
    api.getIntrospectJob.mockResolvedValue({
      jobId: "j2", status: "failed", total: 10, completed: 3,
      error: "无法连接数据源",
    });

    renderPage();
    await user.click(await screen.findByRole("button", { name: /缓存 Schema/i }));

    expect(await screen.findByText("无法连接数据源")).toBeInTheDocument();
    // antd Modal 关闭后 DOM 仍保留 ⇒ 「文本存在」是恒真断言，必须断言弹窗**可见**。
    // 打开动画首帧 opacity=0，故用 waitFor 等动画结束再判可见。
    await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());

    // 终态失败 ⇒ stopPolling：等 2 个轮询周期后调用次数不再增长。
    const callsAtFailure = api.getIntrospectJob.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, TWO_POLL_CYCLES_MS));
    expect(api.getIntrospectJob.mock.calls.length).toBe(callsAtFailure);
  });

  it("关闭弹窗后再次点击「缓存 Schema」：重新启动并显示同一 job 的进度", async () => {
    const user = userEvent.setup();
    // 后端 findRunning 返回同一 job（携带当前进度）：mock 成同一 jobId + 已完成 100。
    const sameJob = { jobId: "j4", status: "running", total: 200, completed: 100 };
    api.startIntrospectJob.mockResolvedValue(sameJob);
    api.getIntrospectJob.mockResolvedValue(sameJob);

    renderPage();
    await user.click(await screen.findByRole("button", { name: /缓存 Schema/i }));
    expect(await screen.findByText(/100\/200/)).toBeInTheDocument();
    expect(api.startIntrospectJob).toHaveBeenCalledTimes(1);

    // 关闭弹窗：后台任务继续；轮询随 activeJob 清空而停止。
    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    // 再次点击必须重新调用接口（不是只复用本地状态），弹窗重开并显示同一 job 的进度。
    await user.click(screen.getByRole("button", { name: /缓存 Schema/i }));
    expect(api.startIntrospectJob).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());
    expect(await screen.findByText(/100\/200/)).toBeInTheDocument();
  });

  it("轮询返回 404（job 已淘汰）：提示任务失效而不是连接中断", async () => {
    const user = userEvent.setup();
    api.startIntrospectJob.mockResolvedValue({
      jobId: "j5", status: "running", total: 10, completed: 0,
    });
    // AppError 形状：轮询接口 reject 带 status=404。
    api.getIntrospectJob.mockRejectedValue(
      Object.assign(new Error("内省任务不存在"), { status: 404 }),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: /缓存 Schema/i }));

    // toast 与弹窗 Alert 同文案 ⇒ getAllByText 容忍多匹配。
    await waitFor(
      () => expect(screen.getAllByText(/内省任务已失效/).length).toBeGreaterThan(0),
      { timeout: 5000 },
    );
    // 404 不应被计成「与服务器连接中断」。
    expect(screen.queryByText(/与服务器连接中断/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());
  });

  it("轮询连续失败 5 次 ⇒ 提示连接中断并停止", async () => {
    const user = userEvent.setup();
    api.startIntrospectJob.mockResolvedValue({
      jobId: "j3", status: "running", total: 10, completed: 0,
    });
    api.getIntrospectJob.mockRejectedValue(new Error("down"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: /缓存 Schema/i }));

    // 提示同时出现在弹窗 Alert 与 message toast 中 ⇒ 用 getAllByText 容忍多匹配。
    // 轮询间隔 1.5s × 5 次失败：真实计时器约需 6s，放宽 waitFor 超时。
    await waitFor(
      () => expect(screen.getAllByText(/与服务器连接中断/).length).toBeGreaterThan(0),
      { timeout: 12000 },
    );
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
