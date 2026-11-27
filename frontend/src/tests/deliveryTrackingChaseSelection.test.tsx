/** 催货闭环的**行选择**回归（feat/delivery-chase-closed-loop）。
 *
 * 两个真实 bug（用户 2026-11-27 实测）：
 * 1. **不同订单的同号行互相串选** —— 明细表 rowKey 只用 `poLine`，
 *    A 单第 1 行与 B 单第 1 行是同一个 key ⇒ 勾一个另一个也亮；
 * 2. **勾了却提示「请先勾选」** —— 明细表 rowKey 与 `createChases` 拼的 key
 *    格式不一致（`poLine` vs `poNo:poLine`）⇒ 选中的行一个都找不到。
 *
 * 本文件是**行为级**回归：真实渲染卡片 → 勾选 → 断言提交给后端的行集合。
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n/i18n";
import { useAuthStore } from "../stores/authStore";
import DeliveryTrackingResultCard, {
  type DeliveryTrackingResult,
} from "../components/copilot/DeliveryTrackingResultCard";

const createChaseFromPending = vi.hoisted(() => vi.fn());
vi.mock("../api/procurementChase", () => ({
  createChaseFromPending: (...args: unknown[]) => createChaseFromPending(...args),
}));

/** 两个订单，**行号都是 1**（bug 1 的触发条件）。 */
const RESULT: DeliveryTrackingResult = {
  dataLayer: "thbi_dwd",
  window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
  summary: {
    totalLines: 2,
    totalOrders: 2,
    orderedQty: 20,
    receivedQty: 0,
    orderArrivalRate: 0,
    qtyArrivalRate: 0,
    allArrivedOrders: 0,
    partialArrivedOrders: 0,
    notArrivedOrders: 2,
    excludedRows: 0,
  },
  bySupplier: [],
  byOrder: [
    {
      poNo: "PO-A",
      supplierCode: "B001",
      supplierName: "供应商甲",
      orderDate: "2026-10-20",
      status: "notArrived",
      totalLines: 1,
      arrivedLines: 0,
      notArrivedLines: 1,
      orderedQty: 10,
      receivedQty: 0,
      hasDirtyLines: false,
      lines: [
        {
          poLine: 1,
          materialCode: "M-A",
          materialDesc: "甲物料",
          orderedQty: 10,
          receivedQty: 0,
          openQty: 10,
          expectedDate: "2026-11-07",
          lastReceiptDate: null,
          status: "notArrived",
          isDirty: false,
        },
      ],
    },
    {
      poNo: "PO-B",
      supplierCode: "B002",
      supplierName: "供应商乙",
      orderDate: "2026-10-21",
      status: "partial",
      totalLines: 1,
      arrivedLines: 0,
      notArrivedLines: 1,
      orderedQty: 10,
      receivedQty: 4,
      hasDirtyLines: false,
      lines: [
        {
          poLine: 1,
          materialCode: "M-B",
          materialDesc: "乙物料",
          orderedQty: 10,
          receivedQty: 4,
          openQty: 6,
          expectedDate: "2026-11-08",
          lastReceiptDate: null,
          status: "partial",
          isDirty: false,
        },
      ],
    },
    {
      poNo: "PO-C",
      supplierCode: "B003",
      supplierName: "供应商丙",
      orderDate: "2026-10-22",
      status: "notArrived",
      totalLines: 1,
      arrivedLines: 0,
      notArrivedLines: 1,
      orderedQty: 1500000,
      receivedQty: 0,
      hasDirtyLines: true,
      lines: [
        {
          poLine: 1,
          materialCode: "M-C",
          materialDesc: "丙物料（脏行）",
          orderedQty: 1500000,
          receivedQty: 0,
          openQty: 1500000,
          expectedDate: "2026-11-09",
          lastReceiptDate: null,
          status: "notArrived",
          isDirty: true,
        },
      ],
    },
  ],
};

function renderCard() {
  return render(
    <ConfigProvider locale={zhCN}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <DeliveryTrackingResultCard result={RESULT} />
        </MemoryRouter>
      </I18nextProvider>
    </ConfigProvider>,
  );
}

describe("催货行选择（跨订单同号行）", () => {
  beforeEach(() => {
    // 催货入口只对 admin 显示 ⇒ 测试身份必须是 admin
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
    createChaseFromPending.mockReset();
    createChaseFromPending.mockResolvedValue({
      requestId: "R",
      drafts: [],
      unresolvedSuppliers: [],
      skippedLines: [],
    });
  });

  it("勾 A 单的行不会连带勾上 B 单的同号行（bug 1）", async () => {
    const user = userEvent.setup();
    renderCard();

    // 两个订单都在「未到货订单」tab；明细行以 Collapse 展开
    // ⚠️ 每张行表还有 antd 的表头「全选」checkbox ⇒ 必须**按行定位**，
    //    不能用下标（会点到表头全选）。
    const rowA = (await screen.findByText("甲物料")).closest("tr")!;
    const rowB = screen.getByText("乙物料").closest("tr")!;
    const checkA = within(rowA).getByRole("checkbox");
    const checkB = within(rowB).getByRole("checkbox");

    await user.click(checkA);

    expect((checkA as HTMLInputElement).checked).toBe(true);
    expect((checkB as HTMLInputElement).checked).toBe(false);
  });

  it("脏行与部分到货行同样可勾可催（用户 2026-11-27：所有订单行都能下催单）", async () => {
    const user = userEvent.setup();
    renderCard();

    // 脏行（openQty 150 万、isDirty）—— checkbox 不得禁用
    const dirtyRow = (await screen.findByText("丙物料（脏行）")).closest("tr")!;
    const dirtyBox = within(dirtyRow).getByRole("checkbox");
    expect((dirtyBox as HTMLInputElement).disabled).toBe(false);

    // 部分到货行 —— 同样不得禁用
    const partialRow = screen.getByText("乙物料").closest("tr")!;
    const partialBox = within(partialRow).getByRole("checkbox");
    expect((partialBox as HTMLInputElement).disabled).toBe(false);

    // 勾脏行 ⇒ 能提交出去（不被前端过滤）
    await user.click(dirtyBox);
    const button = await screen.findByRole("button", { name: /按供应商创建催货单/ });
    await user.click(button);

    expect(createChaseFromPending).toHaveBeenCalledTimes(1);
    const payload = createChaseFromPending.mock.calls[0][0] as {
      lines: Array<{ poNo: string }>;
    };
    expect(payload.lines).toHaveLength(1);
    expect(payload.lines[0]!.poNo).toBe("PO-C");
  });

  it("勾选后能真正提交出行（bug 2：不再提示「请先勾选」）", async () => {
    const user = userEvent.setup();
    renderCard();

    const rowA = (await screen.findByText("甲物料")).closest("tr")!;
    await user.click(within(rowA).getByRole("checkbox"));

    const button = await screen.findByRole("button", { name: /按供应商创建催货单/ });
    await user.click(button);

    // 提交给后端的行 = 被勾的那一行（身份对 poNo+poLine）
    expect(createChaseFromPending).toHaveBeenCalledTimes(1);
    const payload = createChaseFromPending.mock.calls[0][0] as {
      lines: Array<{ poNo: string; poLine: string }>;
    };
    expect(payload.lines).toHaveLength(1);
    expect(payload.lines[0]!.poNo).toBe("PO-A");
    expect(payload.lines[0]!.poLine).toBe("1");
  });
});
