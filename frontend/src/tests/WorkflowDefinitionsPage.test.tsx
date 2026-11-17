import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import WorkflowDefinitionsPage from "../pages/WorkflowDefinitionsPage";
import type { WorkflowDefinitionAdminRead } from "../types/workflow";

const api = vi.hoisted(() => ({
  listWorkflowDefinitionAdmin: vi.fn(),
  createWorkflowDefinition: vi.fn(),
  updateWorkflowDefinition: vi.fn(),
  publishWorkflowDefinition: vi.fn(),
  disableWorkflowDefinition: vi.fn(),
}));

vi.mock("../api/workflow", () => api);

const DRAFT: WorkflowDefinitionAdminRead = {
  id: 5,
  domainCode: "procurement",
  capabilityCode: "purchase_order_approve",
  version: "v1",
  status: "DRAFT",
  definitionJson: {
    riskLevel: "MEDIUM",
    steps: [
      { code: "collect", order: 0, commandType: "po.write" },
      { code: "approve", order: 1, requiresApproval: true },
    ],
    approvalMatrix: [
      {
        matrixCode: "po_medium_manager",
        subjectType: "workflow_instance",
        capabilityCode: "purchase_order_approve",
        riskLevel: "MEDIUM",
        approverRole: "purchase_manager",
        timeoutHours: 24,
      },
    ],
  },
  createdBy: "admin",
  createdTime: "2026-11-17T08:00:00Z",
  updatedTime: "2026-11-17T08:00:00Z",
};

const PUBLISHED: WorkflowDefinitionAdminRead = {
  ...DRAFT,
  id: 6,
  version: "v2",
  status: "PUBLISHED",
};

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <WorkflowDefinitionsPage />
    </ConfigProvider>,
  );
}

describe("WorkflowDefinitionsPage — 定义管理面", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listWorkflowDefinitionAdmin.mockResolvedValue({ rows: [DRAFT], total: 1 });
    api.createWorkflowDefinition.mockResolvedValue(DRAFT);
    api.updateWorkflowDefinition.mockResolvedValue(DRAFT);
    api.publishWorkflowDefinition.mockResolvedValue(PUBLISHED);
    api.disableWorkflowDefinition.mockResolvedValue({ ...DRAFT, status: "DISABLED" });
  });

  it("挂载时拉管理列表并渲染定义的三元组与状态", async () => {
    renderPage();

    await waitFor(() =>
      expect(api.listWorkflowDefinitionAdmin).toHaveBeenCalledWith({
        limit: 20,
        offset: 0,
      }),
    );
    expect(
      await screen.findByText("procurement / purchase_order_approve"),
    ).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("草稿")).toBeInTheDocument();
  });

  it("新建定义提交结构化定义体（步骤 + 审批矩阵）", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("procurement / purchase_order_approve");

    await user.click(screen.getByRole("button", { name: "新建定义" }));
    await user.type(screen.getByLabelText("域代码"), "procurement");
    await user.type(screen.getByLabelText("能力代码"), "purchase_order_approve");
    await user.type(screen.getByLabelText("版本"), "v1");
    await user.type(screen.getByLabelText("步骤编码"), "collect");
    await user.type(screen.getByLabelText("命令类型"), "po.write");
    await user.type(screen.getByLabelText("矩阵编码"), "po_medium_manager");
    await user.type(screen.getByLabelText("审批角色"), "purchase_manager");
    await user.click(screen.getByRole("button", { name: "确 定" }));

    await waitFor(() =>
      expect(api.createWorkflowDefinition).toHaveBeenCalledWith({
        domainCode: "procurement",
        capabilityCode: "purchase_order_approve",
        version: "v1",
        definitionJson: {
          riskLevel: "MEDIUM",
          steps: [{ code: "collect", order: 0, commandType: "po.write" }],
          approvalMatrix: [
            {
              matrixCode: "po_medium_manager",
              subjectType: "workflow_instance",
              capabilityCode: "purchase_order_approve",
              riskLevel: "MEDIUM",
              approverRole: "purchase_manager",
            },
          ],
        },
      }),
    );
  });

  it("编辑 DRAFT 时回填已有定义体并提交更新", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("procurement / purchase_order_approve");

    await user.click(screen.getByRole("button", { name: "编 辑" }));
    await waitFor(() =>
      expect(screen.getByLabelText("域代码")).toHaveValue("procurement"),
    );
    expect(screen.getByLabelText("能力代码")).toHaveValue("purchase_order_approve");
    expect(screen.getByLabelText("版本")).toHaveValue("v1");
    // 审批步骤回填为勾选态（第二步是审批门；每步一个同名勾选框 ⇒ 取全部）
    const approvalBoxes = screen.getAllByLabelText("需要审批");
    expect(approvalBoxes[1]).toBeChecked();
    expect(approvalBoxes[0]).not.toBeChecked();

    await user.click(screen.getByRole("button", { name: "确 定" }));

    await waitFor(() =>
      expect(api.updateWorkflowDefinition).toHaveBeenCalledWith(5, {
        domainCode: "procurement",
        capabilityCode: "purchase_order_approve",
        version: "v1",
        definitionJson: DRAFT.definitionJson,
      }),
    );
  });

  it("发布 DRAFT 调用发布接口并刷新列表", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("procurement / purchase_order_approve");

    await user.click(screen.getByRole("button", { name: "发 布" }));

    await waitFor(() =>
      expect(api.publishWorkflowDefinition).toHaveBeenCalledWith(5),
    );
    await waitFor(() =>
      expect(api.listWorkflowDefinitionAdmin.mock.calls.length).toBeGreaterThanOrEqual(2),
    );
  });

  it("已发布定义只能停用，不能编辑", async () => {
    const user = userEvent.setup();
    api.listWorkflowDefinitionAdmin.mockResolvedValue({ rows: [PUBLISHED], total: 1 });
    renderPage();
    await screen.findByText("v2");

    expect(screen.queryByRole("button", { name: "编 辑" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "发 布" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "停 用" }));
    await waitFor(() =>
      expect(api.disableWorkflowDefinition).toHaveBeenCalledWith(6),
    );
  });

  it("后端 403（非 admin）时展示无权限提示而非空表", async () => {
    api.listWorkflowDefinitionAdmin.mockRejectedValue(
      Object.assign(new Error("需要 admin 角色"), { status: 403 }),
    );
    renderPage();

    expect(await screen.findByText(/需要 admin 角色/)).toBeInTheDocument();
  });
});
