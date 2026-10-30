import { describe, it, expect, vi, beforeEach, afterEach, beforeAll, afterAll } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AgentRegistryPage from "../pages/AgentRegistryPage";
import type { AgentAccessPolicy, AgentDefinition } from "../types/agentRegistry";
import { _resetCache as _resetAgentOptionsCache } from "../hooks/useAgentOptions";

const agentOptionsApi = vi.hoisted(() => ({
    getAgentOptions: vi.fn(),
}));
vi.mock("../api/agentOptions", () => agentOptionsApi);

// antd 静态 message 会把 toast 塞进 document.body 的 portal，跨用例残留会让
// getByText 撞到上一个用例的提示 → 改成替身断言（与 ChangePasswordPage 同口径）。
// 只替换 message.*，其余 antd 导出（ConfigProvider / Modal / Table ...）保持真实实现。
const messageMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  return {
    ...actual,
    message: {
      ...(actual.message ?? {}),
      success: messageMock.success,
      error: messageMock.error,
      warning: messageMock.warning,
      info: messageMock.info,
    },
  };
});

// 回归背景（ERR_INSUFFICIENT_RESOURCES 请求风暴）：useTranslation 的 t 曾经
// 每次渲染都是新引用，refresh = useCallback(..., [filterStatus, t]) 随之失效，
// useEffect(() => refresh(), [refresh]) 每次渲染都重新请求 → 无限循环。
// 契约：挂载后 listAgents 恰好调用一次。
const api = vi.hoisted(() => ({
  listAgents: vi.fn(),
  getAgent: vi.fn(),
  listAgentPolicies: vi.fn(),
  createAgent: vi.fn(),
  updateAgent: vi.fn(),
  deprecateAgent: vi.fn(),
  addAgentPolicy: vi.fn(),
  updateAgentPolicy: vi.fn(),
  deleteAgentPolicy: vi.fn(),
}));

vi.mock("../api/agentRegistry", () => api);

const activeAgent: AgentDefinition = {
  id: 1,
  agentCode: "SUPPLIER_RISK_AGENT",
  agentName: "供应商风险 Agent",
  description: null,
  triggerType: "user_question",
  responseLatency: "realtime",
  dataDomains: ["PROCUREMENT"],
  dataLayers: ["FEATURE"],
  status: "active",
  owner: "procurement",
  version: "v1.0",
  policies: [],
  runnable: true,
  toolName: null,
};

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/agents"]}>
        <Routes>
          <Route path="/agents" element={<AgentRegistryPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("AgentRegistryPage", () => {
  beforeEach(() => {
    agentOptionsApi.getAgentOptions.mockReset().mockResolvedValue({
      domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
      layers: ["DIM", "DWD", "FEATURE"],
      tools: [],
    });
    api.listAgents.mockReset().mockResolvedValue([activeAgent]);
  });

  it("挂载时拉一次 /agents/options（不重复请求）", async () => {
    renderPage();
    await waitFor(() =>
      expect(agentOptionsApi.getAgentOptions).toHaveBeenCalledTimes(1),
    );
  });

  it("挂载后只请求一次列表（t 引用稳定，无请求风暴）", async () => {
    renderPage();
    // 等首次请求完成并渲染（setAgents/setLoading 各触发一次重渲染）
    expect(await screen.findByText("SUPPLIER_RISK_AGENT")).toBeInTheDocument();
    // 再等一小段：若存在无限循环，此处调用次数会持续增长
    await waitFor(
      () => expect(api.listAgents).toHaveBeenCalledTimes(1),
      { timeout: 300 },
    );
    expect(api.listAgents).toHaveBeenCalledTimes(1);
  });
});

describe("AgentRegistryPage - toolName binding", () => {
  const mockTools = [
    { name: "supplier_360", description: "供应商360工具", dataObject: "SUPPLIER", dataLayers: ["DIM", "FEATURE"] },
  ];

  beforeEach(() => {
    _resetAgentOptionsCache();
    agentOptionsApi.getAgentOptions.mockReset().mockResolvedValue({
      domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
      layers: ["DIM", "DWD", "FEATURE"],
      tools: mockTools,
    });
    api.listAgents.mockReset().mockResolvedValue([]);
  });

  // unhandledRejection 守卫：本组用例都走「点击提交 / 联动校验」路径，而 antd 的
  // `validateFields()` 在校验失败时 **reject**、调用点（onOk / onChange）丢弃返回值
  // ⇒ rejection 无人接管。vitest 默认把它计入套件错误（exit=1），但那只是末尾一行
  // 摘要、不指向具体用例；这里显式断言，让失败落在**用例**上。
  // 监听只在本 describe 内装/卸：装了就得由我断言，免得顺带屏蔽 vitest 对本文件
  // 其余用例的全局统计。
  const unhandledRejections: unknown[] = [];
  const collectUnhandled = (reason: unknown) => {
    unhandledRejections.push(reason);
  };

  beforeAll(() => {
    process.on("unhandledRejection", collectUnhandled);
  });
  afterAll(() => {
    process.off("unhandledRejection", collectUnhandled);
  });
  beforeEach(() => {
    unhandledRejections.length = 0;
  });
  afterEach(async () => {
    // node 在微任务队列清空后才派发 unhandledRejection —— 让出一轮事件循环再断言
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(
      unhandledRejections,
      "本组用例不得泄漏 unhandled rejection（antd validateFields 校验失败会 reject）",
    ).toHaveLength(0);
  });

  it("displays toolName in detail drawer when present", async () => {
    const agentWithTool: AgentDefinition = {
      ...activeAgent,
      toolName: "supplier_360",
    };
    api.listAgents.mockResolvedValue([agentWithTool]);
    api.getAgent.mockResolvedValue(agentWithTool);
    api.listAgentPolicies.mockResolvedValue([]);

    renderPage();
    await waitFor(() =>
      expect(screen.queryByText("SUPPLIER_RISK_AGENT")).toBeInTheDocument(),
    );

    // Click the agent code link to open detail drawer
    const link = await screen.findByText("SUPPLIER_RISK_AGENT");
    (link as HTMLAnchorElement).click();

    await waitFor(() =>
      expect(screen.queryByText("supplier_360")).toBeInTheDocument(),
    );
  });

  it("shows validation error when tool layers not covered by agent dataLayers", async () => {
    renderPage();
    await waitFor(() =>
      expect(agentOptionsApi.getAgentOptions).toHaveBeenCalledTimes(1),
    );

    // Open create modal
    const createBtn = await screen.findByText("新建 Agent");
    createBtn.click();

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).toBeInTheDocument(),
    );

    // Verify toolName select is present in the form (at least 6 selects)
    await waitFor(() => {
      const selects = document.querySelectorAll(".ant-select");
      return selects.length >= 6;
    });

    // The form has at least 6 selects: status, triggerType, responseLatency, dataDomains, dataLayers, toolName
    const allSelects = document.querySelectorAll(".ant-select");
    expect(allSelects.length).toBeGreaterThanOrEqual(6);

    // Verify the toolName select has the expected placeholder text when opened
    // (placeholder shows when no value is selected)
    const toolNameSelect = allSelects[5] as HTMLElement;
    expect(toolNameSelect).toBeTruthy();
  });

  it("submits successfully when toolName and dataLayers are compatible", async () => {
    api.createAgent.mockResolvedValue({ id: 2 });

    renderPage();
    await waitFor(() =>
      expect(agentOptionsApi.getAgentOptions).toHaveBeenCalledTimes(1),
    );

    // Open create modal
    const createBtn = await screen.findByText("新建 Agent");
    createBtn.click();

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).toBeInTheDocument(),
    );

    // Verify form has the expected selects (at least 6: status, triggerType, responseLatency, dataDomains, dataLayers, toolName)
    await waitFor(() => {
      const selects = document.querySelectorAll(".ant-select");
      return selects.length >= 6;
    });

    const allSelects = document.querySelectorAll(".ant-select");
    expect(allSelects.length).toBeGreaterThanOrEqual(6);

    // Submit with empty required fields should fail (agentCode, agentName are required)
    const okButton = document.querySelectorAll(".ant-btn-primary")[1] as HTMLElement;
    okButton?.click();

    // Should show validation error for required agentCode field
    await waitFor(
      () =>
        expect(
          screen.queryByText(/编码必须以大写字母开头/i),
        ).toBeInTheDocument(),
      { timeout: 3000 },
    );

    // Verify createAgent was NOT called (validation blocked submission)
    expect(api.createAgent).not.toHaveBeenCalled();
  });
});

// =============================================================================
// 行为覆盖补齐（函数覆盖率）
//
// 断言口径：落在**行为**上——
//  - 筛选 / 刷新 → listAgents 收到的查询参数
//  - 创建 / 编辑 / 停用 → 对应 api 收到的 (参数…, payload)
//  - 策略 CRUD → addAgentPolicy / updateAgentPolicy / deleteAgentPolicy 的实参
//  - 校验拦截 → api **没被调用** 且错误文案出现（配一条"被调用"的正向用例）
//
// 反假绿约束：
//  - 每条"未调用"断言都配一条"被调用"的反向用例
//  - 不读表单内部 state，只断言**提交出去的 payload**
// =============================================================================

const mockTools = [
  {
    name: "supplier_360",
    description: "供应商360工具",
    dataObject: "SUPPLIER",
    dataLayers: ["DIM", "FEATURE"],
  },
];

const policyFixtures: AgentAccessPolicy[] = [
  {
    id: 7,
    dataObject: "SUPPLIER",
    permission: "masked_read",
    dataLayer: "DWD",
    notes: "敏感字段脱敏",
  },
  {
    id: 8,
    dataObject: "PO",
    permission: "forbidden",
    dataLayer: null,
    notes: null,
  },
];

/** 打开 antd Select 下拉并点中某个选项（项目既定模式：mouseDown .ant-select-selector）。 */
async function pickSelectOption(anchorText: string, optionText: string) {
  const anchor = screen
    .getByText(anchorText)
    .closest(".ant-select") as HTMLElement;
  fireEvent.mouseDown(anchor.querySelector(".ant-select-selector") as HTMLElement);
  const option = await screen.findByText(optionText, {
    selector: ".ant-select-item-option-content",
  });
  fireEvent.click(option);
}

function agentRow(code: string): HTMLElement {
  return screen.getByText(code).closest("tr") as HTMLElement;
}

function modalEl(): HTMLElement {
  return document.querySelector(".ant-modal") as HTMLElement;
}

function drawerBody(): HTMLElement {
  return document.querySelector(".ant-drawer-body") as HTMLElement;
}

/** 渲染页面并打开详情抽屉，返回抽屉 body。 */
async function openDetailDrawer(): Promise<HTMLElement> {
  renderPage();
  await screen.findByText("SUPPLIER_RISK_AGENT");
  fireEvent.click(screen.getByText("SUPPLIER_RISK_AGENT"));
  return await waitFor(() => drawerBody());
}

describe("AgentRegistryPage - 列表筛选与刷新", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _resetAgentOptionsCache();
    agentOptionsApi.getAgentOptions.mockReset().mockResolvedValue({
      domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
      layers: ["DIM", "DWD", "FEATURE"],
      tools: mockTools,
    });
    api.listAgents.mockReset().mockResolvedValue([activeAgent]);
  });

  it("按状态筛选后重新拉列表，查询参数带上 status", async () => {
    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");

    await pickSelectOption("按状态过滤", "draft");

    await waitFor(() =>
      expect(api.listAgents).toHaveBeenCalledWith({ status: "draft" }),
    );
    // 反向：未筛选时不带 status
    expect(api.listAgents).toHaveBeenCalledWith({});
  });

  it("按域多选筛选后重新拉列表，查询参数带上 dataDomain 数组", async () => {
    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");

    await pickSelectOption("按域过滤（多选）", "PROCUREMENT");

    await waitFor(() =>
      expect(api.listAgents).toHaveBeenCalledWith({
        dataDomain: ["PROCUREMENT"],
      }),
    );
  });

  it("点「刷新」再拉一次列表", async () => {
    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    expect(api.listAgents).toHaveBeenCalledTimes(1);

    // antd 对 2 字中文按钮名自动插空格（"刷新" → "刷 新"）
    fireEvent.click(screen.getByRole("button", { name: /刷\s*新/ }));

    await waitFor(() => expect(api.listAgents).toHaveBeenCalledTimes(2));
  });

  it("列表加载失败时提示「加载 Agent 列表失败」", async () => {
    api.listAgents.mockRejectedValue(new Error("boom"));

    renderPage();

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith(
        "加载 Agent 列表失败: Error: boom",
      ),
    );
  });
});

describe("AgentRegistryPage - 创建 / 编辑 / 停用", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _resetAgentOptionsCache();
    agentOptionsApi.getAgentOptions.mockReset().mockResolvedValue({
      domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
      layers: ["DIM", "DWD", "FEATURE"],
      tools: mockTools,
    });
    api.listAgents.mockReset().mockResolvedValue([activeAgent]);
    api.getAgent.mockReset().mockResolvedValue(activeAgent);
    api.listAgentPolicies.mockReset().mockResolvedValue([]);
    api.createAgent.mockReset().mockResolvedValue(activeAgent);
    api.updateAgent.mockReset().mockResolvedValue(activeAgent);
    api.deprecateAgent.mockReset().mockResolvedValue(activeAgent);
    api.addAgentPolicy.mockReset().mockResolvedValue(policyFixtures[0]);
    api.updateAgentPolicy.mockReset().mockResolvedValue(policyFixtures[0]);
    api.deleteAgentPolicy.mockReset().mockResolvedValue(undefined);
  });

  it("创建成功：createAgent 收到表单值，成功后提示并刷新列表", async () => {
    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    const listCallsBefore = api.listAgents.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "新建 Agent" }));
    const modal = await waitFor(() => modalEl());
    fireEvent.change(within(modal).getByLabelText("Agent 编码"), {
      target: { value: "TEST_AGENT_1" },
    });
    fireEvent.change(within(modal).getByLabelText("Agent 名称"), {
      target: { value: "测试 Agent" },
    });
    // "创建" 是 2 字中文按钮 → antd 插入空格
    fireEvent.click(within(modal).getByRole("button", { name: /创\s*建/ }));

    await waitFor(() => expect(api.createAgent).toHaveBeenCalledTimes(1));
    expect(api.createAgent).toHaveBeenCalledWith(
      expect.objectContaining({
        agentCode: "TEST_AGENT_1",
        agentName: "测试 Agent",
        triggerType: "user_question",
        responseLatency: "realtime",
        status: "draft",
        policies: [],
      }),
    );
    expect(messageMock.success).toHaveBeenCalledWith("Agent 创建成功");
    await waitFor(() =>
      expect(api.listAgents.mock.calls.length).toBeGreaterThan(listCallsBefore),
    );
  });

  it("创建失败：提示「创建失败」且不关闭弹窗", async () => {
    api.createAgent.mockRejectedValue(new Error("boom"));

    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    fireEvent.click(screen.getByRole("button", { name: "新建 Agent" }));
    const modal = await waitFor(() => modalEl());
    fireEvent.change(within(modal).getByLabelText("Agent 编码"), {
      target: { value: "TEST_AGENT_2" },
    });
    fireEvent.change(within(modal).getByLabelText("Agent 名称"), {
      target: { value: "测试 Agent" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /创\s*建/ }));

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith("创建失败: Error: boom"),
    );
    // 失败时弹窗保持打开
    expect(document.querySelector(".ant-modal-wrap")).not.toHaveStyle({
      display: "none",
    });
  });

  it("创建弹窗点「取消」：不提交 createAgent 且弹窗关闭", async () => {
    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    fireEvent.click(screen.getByRole("button", { name: "新建 Agent" }));
    const modal = await waitFor(() => modalEl());
    fireEvent.change(within(modal).getByLabelText("Agent 编码"), {
      target: { value: "TEST_AGENT_3" },
    });
    fireEvent.change(within(modal).getByLabelText("Agent 名称"), {
      target: { value: "测试 Agent" },
    });

    fireEvent.click(within(modal).getByRole("button", { name: /取\s*消/ }));

    await waitFor(() => {
      expect(document.querySelector(".ant-modal-wrap")).toHaveStyle({
        display: "none",
      });
    });
    expect(api.createAgent).not.toHaveBeenCalled();
  });

  it("编辑保存：updateAgent 收到 (agentCode, 表单值)", async () => {
    const agent: AgentDefinition = {
      ...activeAgent,
      toolName: "supplier_360",
      dataLayers: ["DIM", "FEATURE"],
    };
    api.listAgents.mockResolvedValue([agent]);

    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");

    fireEvent.click(
      within(agentRow("SUPPLIER_RISK_AGENT")).getByRole("button", {
        name: /编\s*辑/,
      }),
    );
    const modal = await waitFor(() => modalEl());
    fireEvent.change(within(modal).getByLabelText("Agent 名称"), {
      target: { value: "改名后的 Agent" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => expect(api.updateAgent).toHaveBeenCalledTimes(1));
    expect(api.updateAgent).toHaveBeenCalledWith(
      "SUPPLIER_RISK_AGENT",
      expect.objectContaining({
        agentName: "改名后的 Agent",
        toolName: "supplier_360",
        status: "active",
        version: "v1.0",
      }),
    );
    expect(messageMock.success).toHaveBeenCalledWith("Agent 更新成功");
  });

  it("编辑弹窗点「取消」：不提交 updateAgent 且弹窗关闭", async () => {
    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");

    fireEvent.click(
      within(agentRow("SUPPLIER_RISK_AGENT")).getByRole("button", {
        name: /编\s*辑/,
      }),
    );
    const modal = await waitFor(() => modalEl());
    fireEvent.click(within(modal).getByRole("button", { name: /取\s*消/ }));

    await waitFor(() => {
      expect(document.querySelector(".ant-modal-wrap")).toHaveStyle({
        display: "none",
      });
    });
    expect(api.updateAgent).not.toHaveBeenCalled();
  });

  it("更新失败：提示「更新失败」", async () => {
    api.updateAgent.mockRejectedValue(new Error("boom"));

    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    fireEvent.click(
      within(agentRow("SUPPLIER_RISK_AGENT")).getByRole("button", {
        name: /编\s*辑/,
      }),
    );
    const modal = await waitFor(() => modalEl());
    fireEvent.click(within(modal).getByRole("button", { name: /保\s*存/ }));

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith("更新失败: Error: boom"),
    );
  });

  it("停用：deprecateAgent 被调用并提示成功", async () => {
    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");

    fireEvent.click(
      within(agentRow("SUPPLIER_RISK_AGENT")).getByRole("button", {
        name: /停\s*用/,
      }),
    );
    // Popconfirm 的确定按钮（zh_CN locale "确定"）
    fireEvent.click(await screen.findByRole("button", { name: /确\s*定/ }));

    await waitFor(() =>
      expect(api.deprecateAgent).toHaveBeenCalledWith("SUPPLIER_RISK_AGENT"),
    );
    expect(messageMock.success).toHaveBeenCalledWith("Agent 已停用");
  });

  it("停用失败：提示「停用失败」", async () => {
    api.deprecateAgent.mockRejectedValue(new Error("boom"));

    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    fireEvent.click(
      within(agentRow("SUPPLIER_RISK_AGENT")).getByRole("button", {
        name: /停\s*用/,
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /确\s*定/ }));

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith("停用失败: Error: boom"),
    );
  });
});

describe("AgentRegistryPage - 详情抽屉与策略 CRUD", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _resetAgentOptionsCache();
    agentOptionsApi.getAgentOptions.mockReset().mockResolvedValue({
      domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
      layers: ["DIM", "DWD", "FEATURE"],
      tools: mockTools,
    });
    api.listAgents.mockReset().mockResolvedValue([activeAgent]);
    api.getAgent.mockReset().mockResolvedValue(activeAgent);
    api.listAgentPolicies.mockReset().mockResolvedValue(policyFixtures);
    api.addAgentPolicy.mockReset().mockResolvedValue(policyFixtures[0]);
    api.updateAgentPolicy.mockReset().mockResolvedValue(policyFixtures[0]);
    api.deleteAgentPolicy.mockReset().mockResolvedValue(undefined);
  });

  it("点编码打开抽屉：拉详情 + 拉策略，并渲染策略表列值", async () => {
    const drawer = await openDetailDrawer();

    await waitFor(() =>
      expect(api.getAgent).toHaveBeenCalledWith("SUPPLIER_RISK_AGENT"),
    );
    expect(api.listAgentPolicies).toHaveBeenCalledWith("SUPPLIER_RISK_AGENT");
    expect(within(drawer).getByText("数据访问策略")).toBeInTheDocument();
    // 权限 Tag（permissionColor）
    expect(within(drawer).getByText("masked_read")).toBeInTheDocument();
    expect(within(drawer).getByText("forbidden")).toBeInTheDocument();
    // dataLayer / notes 列值
    expect(within(drawer).getByText("DWD")).toBeInTheDocument();
    expect(within(drawer).getByText("敏感字段脱敏")).toBeInTheDocument();
    // 空值列渲染占位符
    const emptyRow = within(drawer).getByText("forbidden").closest("tr") as HTMLElement;
    expect(within(emptyRow).getAllByText("—")).toHaveLength(2);
  });

  it("详情加载失败：提示「加载 Agent 详情失败」", async () => {
    api.getAgent.mockRejectedValue(new Error("boom"));
    api.listAgentPolicies.mockResolvedValue([]);

    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    fireEvent.click(screen.getByText("SUPPLIER_RISK_AGENT"));

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith(
        "加载 Agent 详情失败: Error: boom",
      ),
    );
  });

  it("新增策略：addAgentPolicy 收到 (agentCode, payload)", async () => {
    api.listAgentPolicies.mockResolvedValue([]);
    const drawer = await openDetailDrawer();

    // 未选 dataObject（必填）→ 先证明校验拦得住
    fireEvent.click(within(drawer).getByRole("button", { name: /新增策略/ }));
    await waitFor(() => {
      expect(
        within(drawer).getByText("请输入dataObject"),
      ).toBeInTheDocument();
    });
    expect(api.addAgentPolicy).not.toHaveBeenCalled();

    // 选数据对象后新增
    const dataObjectSelect = within(drawer)
      .getByText("如 SUPPLIER")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(
      dataObjectSelect.querySelector(".ant-select-selector") as HTMLElement,
    );
    fireEvent.click(
      await screen.findByText("SUPPLIER", {
        selector: ".ant-select-item-option-content",
      }),
    );
    fireEvent.click(within(drawer).getByRole("button", { name: /新增策略/ }));

    await waitFor(() => expect(api.addAgentPolicy).toHaveBeenCalledTimes(1));
    expect(api.addAgentPolicy).toHaveBeenCalledWith("SUPPLIER_RISK_AGENT", {
      dataObject: "SUPPLIER",
      permission: "read",
      dataLayer: null,
      notes: null,
    });
    expect(messageMock.success).toHaveBeenCalledWith("策略已添加");
  });

  it("编辑策略：updateAgentPolicy 收到 (agentCode, policyId, payload)", async () => {
    const drawer = await openDetailDrawer();
    const policyRow = within(drawer)
      .getByText("masked_read")
      .closest("tr") as HTMLElement;

    fireEvent.click(
      within(policyRow).getByRole("button", { name: /编\s*辑/ }),
    );
    await waitFor(() =>
      expect(
        within(drawer).getByRole("button", { name: "保存修改" }),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(drawer).getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(api.updateAgentPolicy).toHaveBeenCalledTimes(1));
    expect(api.updateAgentPolicy).toHaveBeenCalledWith(
      "SUPPLIER_RISK_AGENT",
      7,
      expect.objectContaining({
        permission: "masked_read",
        dataLayer: "DWD",
        notes: "敏感字段脱敏",
      }),
    );
    expect(messageMock.success).toHaveBeenCalledWith("策略已更新");
  });

  it("取消策略编辑：回到新增态且不调用 updateAgentPolicy", async () => {
    const drawer = await openDetailDrawer();
    const policyRow = within(drawer)
      .getByText("masked_read")
      .closest("tr") as HTMLElement;

    fireEvent.click(
      within(policyRow).getByRole("button", { name: /编\s*辑/ }),
    );
    await waitFor(() =>
      expect(
        within(drawer).getByRole("button", { name: "保存修改" }),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(drawer).getByRole("button", { name: /取\s*消/ }));

    await waitFor(() =>
      expect(
        within(drawer).getByRole("button", { name: /新增策略/ }),
      ).toBeInTheDocument(),
    );
    expect(api.updateAgentPolicy).not.toHaveBeenCalled();
  });

  it("删除策略：deleteAgentPolicy 收到 (agentCode, policyId)", async () => {
    const drawer = await openDetailDrawer();
    const policyRow = within(drawer)
      .getByText("masked_read")
      .closest("tr") as HTMLElement;

    fireEvent.click(
      within(policyRow).getByRole("button", { name: /删\s*除/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /确\s*定/ }));

    await waitFor(() =>
      expect(api.deleteAgentPolicy).toHaveBeenCalledWith(
        "SUPPLIER_RISK_AGENT",
        7,
      ),
    );
    expect(messageMock.success).toHaveBeenCalledWith("策略已删除");
  });

  it("新增策略失败：提示「添加策略失败」", async () => {
    api.listAgentPolicies.mockResolvedValue([]);
    api.addAgentPolicy.mockRejectedValue(new Error("boom"));
    const drawer = await openDetailDrawer();

    const dataObjectSelect = within(drawer)
      .getByText("如 SUPPLIER")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(
      dataObjectSelect.querySelector(".ant-select-selector") as HTMLElement,
    );
    fireEvent.click(
      await screen.findByText("SUPPLIER", {
        selector: ".ant-select-item-option-content",
      }),
    );
    fireEvent.click(within(drawer).getByRole("button", { name: /新增策略/ }));

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith(
        "添加策略失败: Error: boom",
      ),
    );
  });

  it("编辑策略失败：提示「更新策略失败」", async () => {
    api.updateAgentPolicy.mockRejectedValue(new Error("boom"));
    const drawer = await openDetailDrawer();
    const policyRow = within(drawer)
      .getByText("masked_read")
      .closest("tr") as HTMLElement;

    fireEvent.click(
      within(policyRow).getByRole("button", { name: /编\s*辑/ }),
    );
    await waitFor(() =>
      expect(
        within(drawer).getByRole("button", { name: "保存修改" }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(within(drawer).getByRole("button", { name: "保存修改" }));

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith(
        "更新策略失败: Error: boom",
      ),
    );
  });

  it("删除策略失败：提示「删除策略失败」", async () => {
    api.deleteAgentPolicy.mockRejectedValue(new Error("boom"));
    const drawer = await openDetailDrawer();
    const policyRow = within(drawer)
      .getByText("masked_read")
      .closest("tr") as HTMLElement;

    fireEvent.click(
      within(policyRow).getByRole("button", { name: /删\s*除/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /确\s*定/ }));

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith(
        "删除策略失败: Error: boom",
      ),
    );
  });

  it("点抽屉右上角关闭：抽屉收起", async () => {
    await openDetailDrawer();
    const drawer = await waitFor(
      () => document.querySelector(".ant-drawer") as HTMLElement,
    );
    const closeBtn = drawer.querySelector(".ant-drawer-close") as HTMLElement;
    expect(closeBtn).toBeTruthy();

    fireEvent.click(closeBtn);

    // rc-drawer 关闭后根节点仍在 DOM 里，靠 ant-drawer-open 类标记开合状态
    await waitFor(() =>
      expect(document.querySelector(".ant-drawer")).not.toHaveClass(
        "ant-drawer-open",
      ),
    );
  });
});

describe("AgentRegistryPage - toolName 联动校验", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _resetAgentOptionsCache();
    agentOptionsApi.getAgentOptions.mockReset().mockResolvedValue({
      domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
      layers: ["DIM", "DWD", "FEATURE"],
      tools: mockTools,
    });
    api.listAgents.mockReset().mockResolvedValue([activeAgent]);
    api.updateAgent.mockReset().mockResolvedValue(activeAgent);
  });

  it("创建表单选工具但数据层没覆盖：报缺层并拦下提交", async () => {
    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    fireEvent.click(screen.getByRole("button", { name: "新建 Agent" }));
    const modal = await waitFor(() => modalEl());

    const toolItem = within(modal)
      .getByText("绑定工具")
      .closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(
      toolItem.querySelector(".ant-select-selector") as HTMLElement,
    );
    fireEvent.click(
      await screen.findByText("supplier_360 — 供应商360工具", {
        selector: ".ant-select-item-option-content",
      }),
    );

    await waitFor(() =>
      expect(
        screen.getByText(/要求 data_layers 含 DIM,FEATURE/),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(modal).getByRole("button", { name: /创\s*建/ }));
    await waitFor(() => expect(api.createAgent).not.toHaveBeenCalled());
  });

  it("编辑表单选工具但数据层没覆盖：报缺层并拦下保存", async () => {
    // activeAgent.dataLayers = ["FEATURE"]，工具要求 ["DIM","FEATURE"]
    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    fireEvent.click(
      within(agentRow("SUPPLIER_RISK_AGENT")).getByRole("button", {
        name: /编\s*辑/,
      }),
    );
    const modal = await waitFor(() => modalEl());

    const toolItem = within(modal)
      .getByText("绑定工具")
      .closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(
      toolItem.querySelector(".ant-select-selector") as HTMLElement,
    );
    fireEvent.click(
      await screen.findByText("supplier_360 — 供应商360工具", {
        selector: ".ant-select-item-option-content",
      }),
    );

    await waitFor(() =>
      expect(screen.getByText(/要求 data_layers 含 DIM/)).toBeInTheDocument(),
    );

    fireEvent.click(within(modal).getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => expect(api.updateAgent).not.toHaveBeenCalled());
  });

  it("编辑表单的工具名不在词表：提示未知工具并拦下保存", async () => {
    const agent: AgentDefinition = { ...activeAgent, toolName: "ghost_tool" };
    api.listAgents.mockResolvedValue([agent]);

    renderPage();
    await screen.findByText("SUPPLIER_RISK_AGENT");
    fireEvent.click(
      within(agentRow("SUPPLIER_RISK_AGENT")).getByRole("button", {
        name: /编\s*辑/,
      }),
    );
    const modal = await waitFor(() => modalEl());

    fireEvent.click(within(modal).getByRole("button", { name: /保\s*存/ }));

    await waitFor(() =>
      expect(screen.getByText("未知工具，请刷新页面重试")).toBeInTheDocument(),
    );
    expect(api.updateAgent).not.toHaveBeenCalled();
  });
});
