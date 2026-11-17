import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import WorkflowInstancesPage from "../pages/WorkflowInstancesPage";
import type {
  WorkflowDefinitionSummary,
  WorkflowInstanceDetail,
  WorkflowInstanceListItem,
} from "../types/workflow";

const api = vi.hoisted(() => ({
  listWorkflowInstances: vi.fn(),
  getWorkflowInstance: vi.fn(),
  submitWorkflowInstance: vi.fn(),
  cancelWorkflowInstance: vi.fn(),
  listWorkflowDefinitions: vi.fn(),
}));

vi.mock("../api/workflow", () => api);

const INSTANCE: WorkflowInstanceListItem = {
  id: 7,
  definitionId: 3,
  domainCode: "procurement",
  capabilityCode: "purchase_order_approve",
  definitionVersion: "v1",
  status: "WAITING_APPROVAL",
  idempotencyKey: "key-7",
  createdBy: "submitter",
  errorMessage: null,
  createdTime: "2026-11-17T09:00:00Z",
  updatedTime: "2026-11-17T09:01:00Z",
};

const DEFINITION: WorkflowDefinitionSummary = {
  id: 3,
  domainCode: "procurement",
  capabilityCode: "purchase_order_approve",
  version: "v1",
  status: "PUBLISHED",
  createdBy: "admin",
  createdTime: "2026-11-16T00:00:00Z",
};

const DETAIL: WorkflowInstanceDetail = {
  instance: {
    id: 7,
    definitionId: 3,
    status: "WAITING_APPROVAL",
    idempotencyKey: "key-7",
    createdBy: "submitter",
    errorMessage: null,
    createdTime: "2026-11-17T09:00:00Z",
    updatedTime: "2026-11-17T09:01:00Z",
  },
  steps: [
    {
      id: 1,
      stepCode: "collect",
      stepOrder: 0,
      status: "SUCCEEDED",
      errorMessage: null,
      startedAt: "2026-11-17T09:00:00Z",
      finishedAt: "2026-11-17T09:00:10Z",
    },
  ],
  approvals: [
    {
      id: 11,
      instanceId: 7,
      matrixCode: "po_medium_manager",
      subjectType: "workflow_instance",
      subjectId: "7",
      status: "PENDING",
      approverRole: "purchase_manager",
      approverUser: null,
      decisionNote: null,
      dueAt: "2026-11-18T09:00:00Z",
      decidedAt: null,
      createdBy: "submitter",
    },
  ],
};

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <WorkflowInstancesPage />
    </ConfigProvider>,
  );
}

describe("WorkflowInstancesPage — 列表", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listWorkflowInstances.mockResolvedValue({ rows: [INSTANCE], total: 1 });
    api.getWorkflowInstance.mockResolvedValue(DETAIL);
    api.listWorkflowDefinitions.mockResolvedValue([DEFINITION]);
    api.submitWorkflowInstance.mockResolvedValue(DETAIL.instance);
    api.cancelWorkflowInstance.mockResolvedValue(DETAIL.instance);
  });

  it("挂载时按服务端分页参数拉取并渲染能力列", async () => {
    renderPage();

    await waitFor(() =>
      expect(api.listWorkflowInstances).toHaveBeenCalledWith({
        status: undefined,
        limit: 20,
        offset: 0,
      }),
    );
    expect(
      await screen.findByText("procurement / purchase_order_approve"),
    ).toBeInTheDocument();
    expect(screen.getByText("待审批")).toBeInTheDocument();
  });

  it("点击详情拉取实例详情并在抽屉里渲染步骤时间线与审批记录", async () => {
    const user = userEvent.setup();
    renderPage();

    // antd 对两字中文按钮自动插空格 ⇒ 可访问名是「详 情」
    await user.click(await screen.findByRole("button", { name: "详 情" }));

    await waitFor(() => expect(api.getWorkflowInstance).toHaveBeenCalledWith(7));
    expect(await screen.findByText("步骤时间线")).toBeInTheDocument();
    expect(screen.getByText("collect")).toBeInTheDocument();
    expect(screen.getByText("po_medium_manager")).toBeInTheDocument();
  });

  it("提交表单把 JSON 文本解析成对象并带上幂等键", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "提交工作流" }));
    // 提交前先拉已发布定义候选集
    await waitFor(() => expect(api.listWorkflowDefinitions).toHaveBeenCalled());

    await user.click(await screen.findByLabelText("工作流定义"));
    await user.click(
      await screen.findByText("procurement / purchase_order_approve @ v1"),
    );

    // 幂等键由页面预填，但这里显式写入：断言的是「提交时带上用户看到的键」，
    // 而不是预填时序（antd destroyOnHidden 的重挂载在 jsdom 下不可靠复现）。
    const keyBox = screen.getByLabelText("幂等键");
    await user.clear(keyBox);
    await user.type(keyBox, "ui-key-1");

    const requestBox = screen.getByLabelText("请求参数（JSON）");
    await user.clear(requestBox);
    await user.type(requestBox, '{{"amount":100}');

    await user.click(screen.getByRole("button", { name: "确 定" }));

    await waitFor(() => expect(api.submitWorkflowInstance).toHaveBeenCalled());
    const payload = api.submitWorkflowInstance.mock.calls[0][0];
    expect(payload).toMatchObject({
      domainCode: "procurement",
      capabilityCode: "purchase_order_approve",
      version: "v1",
      request: { amount: 100 },
    });
    expect(payload.idempotencyKey).toBe("ui-key-1");
  });

  it("取消实例会带页面统一原因调用取消接口", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "取 消" }));
    await user.click(await screen.findByRole("button", { name: "确 定" }));

    await waitFor(() =>
      expect(api.cancelWorkflowInstance).toHaveBeenCalledWith(7, "用户在页面上取消"),
    );
  });
});
