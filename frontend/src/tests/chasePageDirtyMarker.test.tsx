/** 催货单详情页：**欠交量异常行的红色标识**（feat/delivery-chase-closed-loop）。

用户 2026-11-27：所有行都能催，但欠交量异常（≥ 1,000,000）的行必须
**带红色标识、要求重点核对** —— 那类值多半是源侧录入错误，催出去会把错值带给供应商。
 */

import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n/i18n";
import ProcurementChasePage from "../pages/ProcurementChasePage";
import { useAuthStore } from "../stores/authStore";

const listChaseDrafts = vi.hoisted(() => vi.fn());
const getChaseDraft = vi.hoisted(() => vi.fn());
vi.mock("../api/procurementChase", () => ({
  listChaseDrafts: (...args: unknown[]) => listChaseDrafts(...args),
  getChaseDraft: (...args: unknown[]) => getChaseDraft(...args),
  validateChaseDraft: vi.fn(),
  decideChase: vi.fn(),
  sendChaseDraft: vi.fn(),
}));

const DRAFT = {
  chaseDraftId: "d1",
  version: 1,
  status: "DRAFT",
  requestId: "R1",
  supplierEnterpriseKey: 1,
  supplierCode: "Q1630",
  expectedReplyDeadline: "2026-11-30T00:00:00Z",
  notes: null,
  lineCount: 2,
  createdBy: "admin",
  createdTime: "2026-11-27T00:00:00Z",
  updatedTime: "2026-11-27T00:00:00Z",
};

const DETAIL = {
  draft: DRAFT,
  lines: [
    {
      poNo: "PO1",
      poLine: "1",
      materialCode: "M-1",
      materialDesc: "正常物料",
      orderedQty: 100,
      receivedQty: 0,
      openQty: 100,
      expectedDate: "2026-11-10",
      overdueDays: 3,
      isDirty: false,
    },
    {
      poNo: "PO2",
      poLine: "1",
      materialCode: "M-2",
      materialDesc: "异常物料",
      orderedQty: 1500000,
      receivedQty: 0,
      openQty: 1500000,
      expectedDate: "2026-11-10",
      overdueDays: 5,
      isDirty: true,
    },
  ],
  decisions: [],
};

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <ProcurementChasePage />
        </MemoryRouter>
      </I18nextProvider>
    </ConfigProvider>,
  );
}

describe("催货单详情：欠交量异常行红色标识", () => {
  beforeEach(() => {
    listChaseDrafts.mockReset();
    getChaseDraft.mockReset();
    listChaseDrafts.mockResolvedValue([DRAFT]);
    getChaseDraft.mockResolvedValue(DETAIL);
    useAuthStore.setState({
      token: "t",
      user: {
        id: 1,
        username: "admin",
        displayName: "管理员",
        email: null,
        enabled: true,
        mustChangePassword: false,
        roles: ["admin"],
        organizations: [],
        tenantId: "default",
        lastLoginAt: null,
      },
      mustChangePassword: false,
    });
  });

  it("异常行带红色「数量异常·请重点核对」标识，正常行不带", async () => {
    const user = (await import("@testing-library/user-event")).default.setup();
    renderPage();

    // 打开详情
    const detailLink = await screen.findByText("详情");
    await user.click(detailLink);

    // 异常行有红色标识
    const dirtyTag = await screen.findByTestId("chase-line-dirty");
    expect(dirtyTag.textContent).toContain("数量异常");
    // 头部有异常行计数
    expect(screen.getByText(/1 行数量异常/)).toBeTruthy();
    // 正常行没有该标识（只有一个）
    expect(screen.getAllByTestId("chase-line-dirty")).toHaveLength(1);
  });
});
