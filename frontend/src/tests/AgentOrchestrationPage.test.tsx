import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AgentOrchestrationPage from "../pages/AgentOrchestrationPage";
import type { AgentDefinition } from "../types/agentRegistry";
import type {
  AgentOrchestrationProfileRead,
  AgentOrchestrationToolOptionRead,
} from "../types/agentOrchestration";

const agentApi = vi.hoisted(() => ({
  listAgents: vi.fn(),
  runAgent: vi.fn(),
}));

const orchestrationApi = vi.hoisted(() => ({
  createOrchestration: vi.fn(),
  listOrchestrations: vi.fn(),
  listOrchestrationToolOptions: vi.fn(),
  getOrchestration: vi.fn(),
  updateOrchestration: vi.fn(),
  toggleOrchestration: vi.fn(),
}));

// 旧的 /agents/options 端点：编排页不得用它取工具列表（形状完全不同，且无 handlerRef）。
const legacyOptionsApi = vi.hoisted(() => ({
  getAgentOptions: vi.fn(),
}));

vi.mock("../api/agentRegistry", () => agentApi);
vi.mock("../api/agentOrchestrationApi", () => orchestrationApi);
vi.mock("../api/agentOptions", () => legacyOptionsApi);

const AGENT_CODE = "SUPPLIER_RISK_AGENT";

const agentFixture: AgentDefinition = {
  id: 1,
  agentCode: AGENT_CODE,
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
};

const toolOptionFixtures: AgentOrchestrationToolOptionRead[] = [
  { toolName: "tool_a", description: "工具 A", inputSchema: { type: "object" } },
  { toolName: "tool_b", description: "工具 B", inputSchema: { type: "object" } },
];

const createdProfile: AgentOrchestrationProfileRead = {
  agentCode: AGENT_CODE,
  instructions: "先查风险再汇总",
  enabled: false,
  defaultMaxIterations: 20,
  defaultMaxToolCalls: 30,
  defaultMaxCostUsd: 5,
  defaultTimeoutSeconds: 900,
  maxObservationChars: 50000,
  revision: 1,
  bindings: [
    { toolName: "tool_a", enabled: true, sortOrder: 0 },
    { toolName: "tool_b", enabled: true, sortOrder: 1 },
  ],
};

const existingProfile: AgentOrchestrationProfileRead = {
  ...createdProfile,
  enabled: true,
  revision: 7,
  instructions: "旧指令",
  bindings: [
    { toolName: "tool_z", enabled: true, sortOrder: 0 },
    { toolName: "tool_a", enabled: true, sortOrder: 1 },
  ],
};

const NEW_PROFILE_BUTTON = /新\s*建\s*配\s*置/;
const CREATE_BUTTON = /创\s*建/;
const SAVE_BUTTON = /保\s*存/;
const EDIT_BUTTON = /编\s*辑/;

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <App>
        <MemoryRouter initialEntries={["/agent-orchestrations"]}>
          <AgentOrchestrationPage />
        </MemoryRouter>
      </App>
    </ConfigProvider>,
  );
}

/** 打开弹窗内第 index 个 Select（0 = Agent 编码，1 = 绑定工具）。 */
function openSelect(index: number): void {
  const modal = document.querySelector(".ant-modal-content") as HTMLElement;
  const comboboxes = within(modal).getAllByRole("combobox");
  const selector = comboboxes[index].closest(
    ".ant-select-selector",
  ) as HTMLElement;
  fireEvent.mouseDown(selector);
}

/** 选中当前展开下拉里的选项（按下拉里的可见文本精确匹配）。 */
function selectOption(text: string): void {
  const dropdown = document.querySelector(
    ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
  ) as HTMLElement;
  const option = Array.from(
    dropdown.querySelectorAll(".ant-select-item-option"),
  ).find((el) => el.textContent?.trim() === text);
  if (!option) throw new Error(`下拉里找不到选项：${text}`);
  fireEvent.click(option);
}

function dropdownLabels(): string[] {
  const dropdown = document.querySelector(
    ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
  ) as HTMLElement;
  return Array.from(dropdown.querySelectorAll(".ant-select-item-option")).map(
    (el) => el.textContent?.trim() ?? "",
  );
}

/** 表格里某条配置所在行（限定在表格体内，避免与弹窗里的 Select 回显混淆）。 */
function profileRow(agentCode: string): HTMLElement {
  const tbody = document.querySelector(".ant-table-tbody") as HTMLElement;
  return within(tbody).getByText(agentCode).closest("tr") as HTMLElement;
}

async function findProfileRow(agentCode: string): Promise<HTMLElement> {
  await waitFor(() => expect(profileRow(agentCode)).toBeTruthy());
  return profileRow(agentCode);
}

async function fillCreateForm(instructions: string): Promise<void> {
  openSelect(0);
  await waitFor(() => expect(dropdownLabels().length).toBeGreaterThan(0));
  selectOption(AGENT_CODE);
  fireEvent.change(
    screen.getByPlaceholderText("描述该 Agent 的编排策略与约束"),
    { target: { value: instructions } },
  );
  openSelect(1);
  selectOption("tool_a");
  selectOption("tool_b");
}

describe("AgentOrchestrationPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    agentApi.listAgents.mockResolvedValue([agentFixture]);
    agentApi.runAgent.mockResolvedValue(undefined);
    orchestrationApi.listOrchestrations.mockResolvedValue([]);
    orchestrationApi.listOrchestrationToolOptions.mockResolvedValue(
      toolOptionFixtures,
    );
    orchestrationApi.createOrchestration.mockResolvedValue(createdProfile);
    orchestrationApi.updateOrchestration.mockResolvedValue({
      ...existingProfile,
      revision: 8,
    });
    orchestrationApi.toggleOrchestration.mockResolvedValue({
      ...createdProfile,
      enabled: true,
      revision: 2,
    });
  });

  it("加载 Agent 与只读工具选项，并按所选工具 + 数值默认值创建配置", async () => {
    renderPage();
    await waitFor(() => expect(agentApi.listAgents).toHaveBeenCalled());
    await waitFor(() =>
      expect(orchestrationApi.listOrchestrationToolOptions).toHaveBeenCalled(),
    );

    fireEvent.click(screen.getByRole("button", { name: NEW_PROFILE_BUTTON }));
    await fillCreateForm("先查风险再汇总");
    fireEvent.click(screen.getByRole("button", { name: CREATE_BUTTON }));

    await waitFor(() =>
      expect(orchestrationApi.createOrchestration).toHaveBeenCalledWith({
        agentCode: AGENT_CODE,
        instructions: "先查风险再汇总",
        toolNames: ["tool_a", "tool_b"],
        defaultMaxIterations: 20,
        defaultMaxToolCalls: 30,
        defaultMaxCostUsd: 5,
        defaultTimeoutSeconds: 900,
        maxObservationChars: 50000,
      }),
    );
  });

  it("新建配置默认为停用，必须显式切换才启用", async () => {
    renderPage();
    await waitFor(() => expect(agentApi.listAgents).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: NEW_PROFILE_BUTTON }));
    await fillCreateForm("先查风险再汇总");
    fireEvent.click(screen.getByRole("button", { name: CREATE_BUTTON }));

    await waitFor(() =>
      expect(orchestrationApi.createOrchestration).toHaveBeenCalled(),
    );
    const row = await findProfileRow(AGENT_CODE);
    const toggle = within(row).getByRole("switch");
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(within(row).getByText("未启用")).toBeInTheDocument();
    expect(orchestrationApi.toggleOrchestration).not.toHaveBeenCalled();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(orchestrationApi.toggleOrchestration).toHaveBeenCalledWith(
        AGENT_CODE,
        { enabled: true, revision: 1 },
      ),
    );
  });

  it("编辑配置提交 revision、指令、有序 toolNames 与全部默认值", async () => {
    orchestrationApi.listOrchestrations.mockResolvedValue([existingProfile]);
    renderPage();
    const row = await findProfileRow(AGENT_CODE);

    fireEvent.click(within(row).getByRole("button", { name: EDIT_BUTTON }));

    const textarea = (await screen.findByPlaceholderText(
      "描述该 Agent 的编排策略与约束",
    )) as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.value).toBe("旧指令"));
    fireEvent.change(textarea, { target: { value: "新指令" } });

    fireEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));

    await waitFor(() =>
      expect(orchestrationApi.updateOrchestration).toHaveBeenCalledWith(
        AGENT_CODE,
        {
          instructions: "新指令",
          toolNames: ["tool_z", "tool_a"],
          defaultMaxIterations: 20,
          defaultMaxToolCalls: 30,
          defaultMaxCostUsd: 5,
          defaultTimeoutSeconds: 900,
          maxObservationChars: 50000,
          revision: 7,
        },
      ),
    );
  });

  it("编辑时 revision 冲突（409）→ 友好冲突提示，而非通用失败提示", async () => {
    orchestrationApi.listOrchestrations.mockResolvedValue([existingProfile]);
    orchestrationApi.updateOrchestration.mockRejectedValue(
      Object.assign(new Error("conflict"), { status: 409 }),
    );
    renderPage();
    const row = await findProfileRow(AGENT_CODE);

    fireEvent.click(within(row).getByRole("button", { name: EDIT_BUTTON }));
    fireEvent.click(
      await screen.findByRole("button", { name: SAVE_BUTTON }),
    );

    expect(
      await screen.findByText("配置已被他人修改，请刷新后重试"),
    ).toBeInTheDocument();
    expect(screen.queryByText("更新编排配置失败")).toBeNull();
  });

  it("工具选项只来自编排工具接口，不依赖旧的 /agents/options", async () => {
    renderPage();
    await waitFor(() =>
      expect(orchestrationApi.listOrchestrationToolOptions).toHaveBeenCalled(),
    );

    fireEvent.click(screen.getByRole("button", { name: NEW_PROFILE_BUTTON }));
    openSelect(1);
    await waitFor(() => expect(dropdownLabels().length).toBeGreaterThan(0));

    expect(dropdownLabels()).toEqual(["tool_a", "tool_b"]);
    expect(document.body.textContent).not.toContain("handlerRef");
    expect(legacyOptionsApi.getAgentOptions).not.toHaveBeenCalled();
  });

  it("工具列表加载失败 → 页面提示错误", async () => {
    orchestrationApi.listOrchestrationToolOptions.mockRejectedValue(
      new Error("boom"),
    );
    renderPage();
    expect(
      await screen.findByText("可用工具列表加载失败"),
    ).toBeInTheDocument();
  });

  it("创建失败 → 提示错误", async () => {
    orchestrationApi.createOrchestration.mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() => expect(agentApi.listAgents).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: NEW_PROFILE_BUTTON }));
    await fillCreateForm("先查风险再汇总");
    fireEvent.click(screen.getByRole("button", { name: CREATE_BUTTON }));

    expect(await screen.findByText("创建编排配置失败")).toBeInTheDocument();
  });

  it("表单校验失败不产生未处理的 Promise 拒绝，且不会调用创建接口", async () => {
    const rejections: unknown[] = [];
    const onUnhandled = (event: PromiseRejectionEvent) => {
      rejections.push(event.reason);
    };
    window.addEventListener("unhandledrejection", onUnhandled);
    try {
      renderPage();
      await waitFor(() => expect(agentApi.listAgents).toHaveBeenCalled());

      fireEvent.click(screen.getByRole("button", { name: NEW_PROFILE_BUTTON }));
      fireEvent.click(screen.getByRole("button", { name: CREATE_BUTTON }));

      expect(await screen.findByText("请选择 Agent")).toBeInTheDocument();
      expect(orchestrationApi.createOrchestration).not.toHaveBeenCalled();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(rejections).toHaveLength(0);
    } finally {
      window.removeEventListener("unhandledrejection", onUnhandled);
    }
  });

  it("不会调用旧的 runAgent 接口", async () => {
    renderPage();
    await waitFor(() => expect(agentApi.listAgents).toHaveBeenCalled());
    expect(agentApi.runAgent).not.toHaveBeenCalled();
  });

  it("工具列表为空 → 渲染空状态，且不伪造任何工具选项", async () => {
    orchestrationApi.listOrchestrationToolOptions.mockResolvedValue([]);
    renderPage();
    await waitFor(() =>
      expect(orchestrationApi.listOrchestrationToolOptions).toHaveBeenCalled(),
    );

    fireEvent.click(screen.getByRole("button", { name: NEW_PROFILE_BUTTON }));
    expect(await screen.findByText("暂无可用的只读工具")).toBeInTheDocument();

    openSelect(1);
    expect(document.querySelectorAll(".ant-select-item-option")).toHaveLength(0);
  });

  it("更新返回 422 → 展示具体校验原因，而非通用失败文案", async () => {
    orchestrationApi.listOrchestrations.mockResolvedValue([existingProfile]);
    orchestrationApi.updateOrchestration.mockRejectedValue(
      Object.assign(new Error("工具不存在：tool_x"), { status: 422 }),
    );
    renderPage();
    const row = await findProfileRow(AGENT_CODE);

    fireEvent.click(within(row).getByRole("button", { name: EDIT_BUTTON }));
    fireEvent.click(await screen.findByRole("button", { name: SAVE_BUTTON }));

    expect(
      await screen.findByText("参数校验未通过：工具不存在：tool_x"),
    ).toBeInTheDocument();
    expect(screen.queryByText("更新编排配置失败")).toBeNull();
  });

  it("创建时 Agent 不存在（404）→ 展示专属文案，而非通用失败文案", async () => {
    orchestrationApi.createOrchestration.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404 }),
    );
    renderPage();
    await waitFor(() => expect(agentApi.listAgents).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: NEW_PROFILE_BUTTON }));
    await fillCreateForm("先查风险再汇总");
    fireEvent.click(screen.getByRole("button", { name: CREATE_BUTTON }));

    expect(
      await screen.findByText("找不到对应的 Agent 或编排配置"),
    ).toBeInTheDocument();
    expect(screen.queryByText("创建编排配置失败")).toBeNull();
  });

  it("切换启用状态遇到 409 → 展示冲突文案，并可刷新列表重试", async () => {
    orchestrationApi.listOrchestrations.mockResolvedValue([existingProfile]);
    orchestrationApi.toggleOrchestration.mockRejectedValue(
      Object.assign(new Error("conflict"), { status: 409 }),
    );
    renderPage();
    const row = await findProfileRow(AGENT_CODE);

    fireEvent.click(within(row).getByRole("switch"));
    expect(
      await screen.findByText("配置已被他人修改，请刷新后重试"),
    ).toBeInTheDocument();

    const callsBeforeRefresh =
      orchestrationApi.listOrchestrations.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: /刷\s*新/ }));
    await waitFor(() =>
      expect(
        orchestrationApi.listOrchestrations.mock.calls.length,
      ).toBeGreaterThan(callsBeforeRefresh),
    );
  });

  it("更新被 ACL 拒绝（403）→ 展示权限文案，而非通用失败文案", async () => {
    orchestrationApi.listOrchestrations.mockResolvedValue([existingProfile]);
    orchestrationApi.updateOrchestration.mockRejectedValue(
      Object.assign(new Error("forbidden"), { status: 403 }),
    );
    renderPage();
    const row = await findProfileRow(AGENT_CODE);

    fireEvent.click(within(row).getByRole("button", { name: EDIT_BUTTON }));
    fireEvent.click(await screen.findByRole("button", { name: SAVE_BUTTON }));

    expect(
      await screen.findByText("没有权限修改该 Agent 的编排配置"),
    ).toBeInTheDocument();
    expect(screen.queryByText("更新编排配置失败")).toBeNull();
  });

  it("拒绝值不是 Error（null）→ 仍给出提示，不抛 TypeError", async () => {
    orchestrationApi.createOrchestration.mockRejectedValue(null);
    renderPage();
    await waitFor(() => expect(agentApi.listAgents).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: NEW_PROFILE_BUTTON }));
    await fillCreateForm("先查风险再汇总");
    fireEvent.click(screen.getByRole("button", { name: CREATE_BUTTON }));

    expect(await screen.findByText("创建编排配置失败")).toBeInTheDocument();
  });

  it("预算输入的上限与后端系统天花板一致", async () => {
    renderPage();
    await waitFor(() => expect(agentApi.listAgents).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: NEW_PROFILE_BUTTON }));
    const modal = (await screen.findByRole("dialog")) as HTMLElement;
    const spinbuttons = within(modal).getAllByRole("spinbutton");
    expect(
      spinbuttons.map((input) => input.getAttribute("aria-valuemax")),
    ).toEqual(["20", "30", "5", "900", "50000"]);
  });
});
