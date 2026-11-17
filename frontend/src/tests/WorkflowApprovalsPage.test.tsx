import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import WorkflowApprovalsPage from "../pages/WorkflowApprovalsPage";
import type { WorkflowApprovalListItem } from "../types/workflow";

const api = vi.hoisted(() => ({
  listMyApprovals: vi.fn(),
  decideWorkflowApproval: vi.fn(),
}));

vi.mock("../api/workflow", () => api);

const TASK: WorkflowApprovalListItem = {
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
  domainCode: "procurement",
  capabilityCode: "purchase_order_approve",
  definitionVersion: "v1",
  instanceStatus: "WAITING_APPROVAL",
  instanceCreatedBy: "submitter",
};

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <WorkflowApprovalsPage />
    </ConfigProvider>,
  );
}

describe("WorkflowApprovalsPage — 我的待办", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listMyApprovals.mockResolvedValue({ rows: [TASK], total: 1 });
    api.decideWorkflowApproval.mockResolvedValue({
      ...TASK,
      status: "APPROVED",
      approverUser: "lead",
    });
  });

  it("挂载时只拉 PENDING 待办并渲染实例上下文", async () => {
    renderPage();

    await waitFor(() =>
      expect(api.listMyApprovals).toHaveBeenCalledWith({
        status: "PENDING",
        limit: 20,
        offset: 0,
      }),
    );
    expect(
      await screen.findByText("procurement / purchase_order_approve"),
    ).toBeInTheDocument();
    expect(screen.getByText("po_medium_manager")).toBeInTheDocument();
  });

  it("通过审批时提交 APPROVE 与审批意见，并重新拉取待办", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "通 过" }));
    await user.type(screen.getByLabelText("审批意见"), "同意采购");
    await user.click(screen.getByRole("button", { name: "确 定" }));

    await waitFor(() =>
      expect(api.decideWorkflowApproval).toHaveBeenCalledWith(11, {
        decision: "APPROVE",
        note: "同意采购",
      }),
    );
    // 决策后必须重新拉取：否则同一任务仍留在列表上，可被重复点
    await waitFor(() =>
      expect(api.listMyApprovals.mock.calls.length).toBeGreaterThanOrEqual(2),
    );
  });

  it("驳回必须填写意见，未填写时不发请求", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "驳 回" }));
    await user.click(screen.getByRole("button", { name: "确 定" }));

    expect(await screen.findByText("驳回必须填写意见")).toBeInTheDocument();
    expect(api.decideWorkflowApproval).not.toHaveBeenCalled();
  });

  it("后端 403 APPROVAL_REAL_AUTH_REQUIRED 时展示登录提示而非空表", async () => {
    api.listMyApprovals.mockRejectedValue(
      Object.assign(new Error("审批决策与待办列表要求真实登录身份"), {
        status: 403,
        detail: "APPROVAL_REAL_AUTH_REQUIRED",
      }),
    );
    renderPage();

    expect(
      await screen.findByText(/待办审批要求真实登录身份/),
    ).toBeInTheDocument();
  });
});
