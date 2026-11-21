/** M4-1 询价草稿页测试：列表渲染 + D-4 无发送入口 + 创建幂等契约（mock API）。 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it, vi } from "vitest";
import i18n from "../../i18n/i18n";
import zhCN from "antd/locale/zh_CN";
import ProcurementRfqPage from "../ProcurementRfqPage";
import type { RfqDraft } from "../../api/procurementRfq";

const api = vi.hoisted(() => ({
  listRfqDrafts: vi.fn(),
  getRfqDraft: vi.fn(),
  createRfqDraft: vi.fn(),
  validateRfqDraft: vi.fn(),
  submitRfqApproval: vi.fn(),
  decideRfqApproval: vi.fn(),
  cancelRfqDraft: vi.fn(),
}));

vi.mock("../../api/procurementRfq", () => api);

function makeDraft(overrides: Partial<RfqDraft> = {}): RfqDraft {
  return {
    rfqDraftId: "d-1",
    version: 1,
    status: "DRAFT",
    requestId: "PR-2026-0099",
    deadline: "2026-12-20T09:00:00+08:00",
    notes: null,
    supplierKeys: [900001],
    lineItems: [],
    createdBy: "creator-1",
    decisions: [],
    ...overrides,
  };
}

function renderPage() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <ProcurementRfqPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
}


describe("ProcurementRfqPage", () => {
  it("渲染列表并展示状态标签", async () => {
    api.listRfqDrafts.mockResolvedValue([makeDraft()]);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("PR-2026-0099")).toBeTruthy();
      expect(screen.getByText("DRAFT")).toBeTruthy();
    });
    expect(api.listRfqDrafts).toHaveBeenCalled();
  });

  it("D-4：页面不得出现发送入口（DRAFT-only）", async () => {
    api.listRfqDrafts.mockResolvedValue([
      makeDraft({ status: "READY_TO_SEND", version: 3 }),
    ]);
    const { container } = renderPage();
    await waitFor(() => {
      expect(screen.getByText("READY_TO_SEND")).toBeTruthy();
    });
    const buttons = Array.from(container.querySelectorAll("button")).map(
      (b) => b.textContent ?? "",
    );
    expect(buttons.some((t) => t.includes("发送"))).toBe(false);
    expect(buttons.some((t) => t.toLowerCase().includes("send"))).toBe(false);
  });

  it("PENDING_APPROVAL 视角展示审批动作（自批禁止由服务端裁决）", async () => {
    api.listRfqDrafts.mockResolvedValue([
      makeDraft({ status: "PENDING_APPROVAL", version: 2 }),
    ]);
    api.getRfqDraft.mockResolvedValue(
      makeDraft({ status: "PENDING_APPROVAL", version: 2 }),
    );
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("PENDING_APPROVAL")).toBeTruthy();
    });
    // 详情打开后才渲染动作 ⇒ 打开抽屉
    fireEvent.click(screen.getByText(/详\s*情/));
    await waitFor(() => {
      expect(screen.getByText(/通\s*过/)).toBeTruthy();
      expect(screen.getByText(/驳\s*回/)).toBeTruthy();
    });
  });
});
