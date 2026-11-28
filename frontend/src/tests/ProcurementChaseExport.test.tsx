/** 催货单导出下载（Task 3）：请求形状 + 两种错误包络 + 文件名来源 + 菜单入口。
 *
 * 背景：
 * - httpClient 走 Bearer token ⇒ `<a download>` 带不了 header ⇒ 必须 blob + objectURL；
 * - 后端**两种错误包络并存**：400 走 `HTTPException` ⇒ `{"detail": "..."}`；
 *   404 走统一包络 ⇒ `{"success": false, "error": "..."}` ⇒ 前端提示要兼容两者。
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCNLocale from "antd/locale/zh_CN";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";

import i18n from "../i18n/i18n";
import { zhCN } from "../i18n/zh-CN";
import { enUS } from "../i18n/en-US";
import { useAuthStore } from "../stores/authStore";
import DeliveryTrackingResultCard, {
  type DeliveryTrackingResult,
} from "../components/copilot/DeliveryTrackingResultCard";
import ProcurementChasePage from "../pages/ProcurementChasePage";
import type { ChaseDraft } from "../api/procurementChase";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { downloadChaseDraft } from "../api/procurementChase";

/** 下载时创建的 <a download>（文件名断言用）。 */
let anchors: HTMLAnchorElement[] = [];
let originalCreateElement: typeof document.createElement;

describe("催货单导出下载", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    httpMock.get.mockResolvedValue({ data: new Blob(["x"]) });
    // jsdom 无 createObjectURL
    (globalThis as unknown as { URL: { createObjectURL: () => string } }).URL.createObjectURL =
      () => "blob:mock";
    (globalThis as unknown as { URL: { revokeObjectURL: (u: string) => void } }).URL.revokeObjectURL =
      () => undefined;
    // jsdom 点 <a href="blob:..."> 会尝试导航（未实现 ⇒ 报噪声错）⇒ 拦掉 click
    anchors = [];
    originalCreateElement = document.createElement.bind(document);
    document.createElement = ((tagName: string) => {
      const element = originalCreateElement(tagName);
      if (tagName === "a") {
        (element as HTMLAnchorElement).click = vi.fn();
        anchors.push(element as HTMLAnchorElement);
      }
      return element;
    }) as typeof document.createElement;
  });

  afterEach(() => {
    document.createElement = originalCreateElement;
  });

  it("用 blob 响应下载并带正确 format 参数", async () => {
    await downloadChaseDraft("d-1", "xlsx");
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/procurement/chase/drafts/d-1/export");
    expect(call[1].params).toEqual({ format: "xlsx" });
    expect(call[1].responseType).toBe("blob");
  });

  it("400 的 HTTPException 包络（{\"detail\"}）⇒ 抛出该文案", async () => {
    httpMock.get.mockResolvedValue({
      status: 400,
      data: new Blob([JSON.stringify({ detail: "format 只支持 ['docx', 'xlsx']" })], {
        type: "application/json",
      }),
    });

    await expect(downloadChaseDraft("d-1", "xlsx")).rejects.toThrow(
      /format 只支持/,
    );
  });

  it("404 的统一包络（{\"success\": false, \"error\"}）⇒ 抛出该文案", async () => {
    httpMock.get.mockResolvedValue({
      status: 404,
      data: new Blob([JSON.stringify({ success: false, error: "催货单不存在: d-1" })], {
        type: "application/json",
      }),
    });

    await expect(downloadChaseDraft("d-1", "xlsx")).rejects.toThrow(/催货单不存在/);
  });

  it("错误体读不出文案 ⇒ 抛通用兜底文案（不静默）", async () => {
    httpMock.get.mockResolvedValue({
      status: 403,
      data: new Blob(["<html>forbidden</html>"], { type: "text/html" }),
    });

    await expect(downloadChaseDraft("d-1", "docx")).rejects.toThrow(/下载失败/);
  });

  it("请求被拒（401/5xx/网络异常）⇒ 抛通用兜底文案（拦截器已丢弃原始响应）", async () => {
    httpMock.get.mockRejectedValue(new Error("Request failed with status code 401"));

    await expect(downloadChaseDraft("d-1", "xlsx")).rejects.toThrow(/下载失败/);
  });

  it("文件名取 Content-Disposition 的 RFC5987 名（优先于 ASCII 回退名）", async () => {
    httpMock.get.mockResolvedValue({
      status: 200,
      data: new Blob(["x"]),
      headers: {
        "content-disposition":
          "attachment; filename=\"chase-ab12cd34.xlsx\"; filename*=UTF-8''%E5%82%AC%E8%B4%A7%E5%8D%95_ab12cd34.xlsx",
      },
    });

    await downloadChaseDraft("ab12cd34-full-id", "xlsx");

    expect(anchors).toHaveLength(1);
    expect(anchors[0].download).toBe("催货单_ab12cd34.xlsx");
  });

  it("没有 Content-Disposition ⇒ 本地按催货单 id 前 8 位拼文件名", async () => {
    httpMock.get.mockResolvedValue({ status: 200, data: new Blob(["x"]) });

    await downloadChaseDraft("ab12cd34-full-id", "docx");

    expect(anchors[0].download).toBe("催货单_ab12cd34.docx");
  });
});

/** 菜单入口契约：seed ↔ i18n ↔ 前端路由三方对齐（否则点了 404 / 显示原始 key）。 */
describe("催货单菜单入口", () => {
  const REPO_ROOT = resolve(__dirname, "..", "..", "..");

  it("seed 里有 item.procurementChase（sort_order 2750，path /procurement/chase）", () => {
    const seed = readFileSync(
      resolve(REPO_ROOT, "backend/scripts/seed_menu_config.py"),
      "utf8",
    );
    expect(seed).toContain('"code": "item.procurementChase"');
    expect(seed).toContain('"sort_order": 2750');
    expect(seed).toContain('"path": "/procurement/chase"');
  });

  it("App.tsx 有 /procurement/chase 路由（菜单点了不 404）", () => {
    const appSrc = readFileSync(resolve(REPO_ROOT, "frontend/src/App.tsx"), "utf8");
    expect(appSrc).toContain('path="procurement/chase"');
  });

  it("菜单 label 与下载按钮文案在 zh-CN / en-US 都有译文", () => {
    expect(zhCN.menu.item.procurementChase).toBe("催货单");
    expect(enUS.menu.item.procurementChase).toBe("Chase Orders");
    expect(zhCN.chase.export.excel).toBe("Excel");
    expect(enUS.chase.export.excel).toBe("Excel");
    expect(zhCN.chase.export.word).toBe("Word");
    expect(enUS.chase.export.word).toBe("Word");
    expect(zhCN.chase.viewList).toBe("查看催货单");
    expect(enUS.chase.viewList).toBe("View chase orders");
  });
});

/** 催货单创建成功 ⇒ 成功提示里带「查看催货单」跳转链接。 */
const RESULT: DeliveryTrackingResult = {
  window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
  summary: {
    totalLines: 1,
    totalOrders: 1,
    orderedQty: 10,
    receivedQty: 0,
    orderArrivalRate: 0,
    qtyArrivalRate: 0,
    allArrivedOrders: 0,
    partialArrivedOrders: 0,
    notArrivedOrders: 1,
    excludedRows: 0,
  },
  bySupplier: [],
  byOrder: [
    {
      poNo: "PO-A",
      supplierCode: "B001",
      supplierName: "供应商甲",
      status: "notArrived",
      totalLines: 1,
      arrivedLines: 0,
      notArrivedLines: 1,
      orderedQty: 10,
      receivedQty: 0,
      lines: [
        {
          poLine: 1,
          materialCode: "M-A",
          materialDesc: "甲物料",
          orderedQty: 10,
          receivedQty: 0,
          openQty: 10,
          status: "notArrived",
        },
      ],
    },
  ],
};

describe("创建催货单后的跳转链接", () => {
  beforeEach(() => {
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
    httpMock.post.mockResolvedValue({
      data: {
        requestId: "R",
        drafts: [{ chaseDraftId: "d-1", supplierCode: "B001" }],
        unresolvedSuppliers: [],
        skippedLines: [],
      },
    });
  });

  it("成功提示里有指向催货单列表的链接", async () => {
    const user = userEvent.setup();
    render(
      <ConfigProvider locale={zhCNLocale}>
        <I18nextProvider i18n={i18n}>
          <MemoryRouter>
            <DeliveryTrackingResultCard result={RESULT} />
          </MemoryRouter>
        </I18nextProvider>
      </ConfigProvider>,
    );

    const row = (await screen.findByText("甲物料")).closest("tr")!;
    await user.click(within(row).getByRole("checkbox"));

    const button = await screen.findByRole("button", { name: /按供应商创建催货单/ });
    await user.click(button);

    const link = await screen.findByText("查看催货单");
    expect(link.getAttribute("href")).toBe("/procurement/chase");
  });
});

/** 催货单页下载按钮：**用被点那一行的 draftId** 调导出端点。
 *
 * 缺口（2026-11-28 终审）：原测试只测 `downloadChaseDraft` 函数本身，
 * 没覆盖「页面某行的按钮 → 该行的 draftId」这条链路 ⇒ 传错行 id 的 bug
 * （下错别人的催货单）测不出来。
 */
const DRAFT_A_ID = "aaaa1111-0000-0000-0000-000000000001";
const DRAFT_B_ID = "bbbb2222-0000-0000-0000-000000000002";

function _draft(chaseDraftId: string, supplierCode: string): ChaseDraft {
  return {
    chaseDraftId,
    version: 1,
    status: "DRAFT",
    requestId: `R-${chaseDraftId.slice(0, 4)}`,
    supplierEnterpriseKey: 1,
    supplierCode,
    expectedReplyDeadline: "2026-12-01T00:00:00Z",
    notes: null,
    lineCount: 1,
    createdBy: "admin",
    createdTime: "2026-11-28T00:00:00Z",
    updatedTime: "2026-11-28T00:00:00Z",
  };
}

describe("催货单页下载按钮", () => {
  /** 本 describe 自己的 createElement 替身基线（afterEach 还原，避免泄漏到别的用例）。 */
  let pristineCreateElement: typeof document.createElement;

  afterEach(() => {
    if (pristineCreateElement) {
      document.createElement = pristineCreateElement;
    }
  });

  beforeEach(() => {
    vi.clearAllMocks();
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
    (globalThis as unknown as { URL: { createObjectURL: () => string } }).URL.createObjectURL =
      () => "blob:mock";
    (globalThis as unknown as { URL: { revokeObjectURL: (u: string) => void } }).URL.revokeObjectURL =
      () => undefined;
    // jsdom 点 <a href="blob:..."> 会尝试导航（未实现 ⇒ 噪声错）⇒ 拦掉 click
    pristineCreateElement = document.createElement.bind(document);
    document.createElement = ((tagName: string) => {
      const element = pristineCreateElement(tagName);
      if (tagName === "a") {
        (element as HTMLAnchorElement).click = vi.fn();
      }
      return element;
    }) as typeof document.createElement;
    // 列表接口返回两张单；导出接口返回空 blob
    httpMock.get.mockImplementation((url: string) => {
      if (String(url).includes("/export")) {
        return Promise.resolve({
          status: 200,
          data: new Blob(["x"]),
          headers: {},
        });
      }
      return Promise.resolve({
        data: [_draft(DRAFT_A_ID, "Q1000"), _draft(DRAFT_B_ID, "Q2000")],
      });
    });
  });

  it("点第二行的 Excel 按钮 ⇒ 用**那一行**的 draftId 调导出端点", async () => {
    const user = userEvent.setup();
    render(
      <ConfigProvider locale={zhCNLocale}>
        <I18nextProvider i18n={i18n}>
          <MemoryRouter>
            <ProcurementChasePage />
          </MemoryRouter>
        </I18nextProvider>
      </ConfigProvider>,
    );

    // 第二行的单号前缀（表格里展示 id 前 8 位）
    const idCell = await screen.findByText(DRAFT_B_ID.slice(0, 8));
    const row = idCell.closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Excel" }));

    const exportCalls = httpMock.get.mock.calls.filter((call) =>
      String(call[0]).includes("/export"),
    );
    expect(exportCalls).toHaveLength(1);
    expect(exportCalls[0][0]).toBe(
      `/procurement/chase/drafts/${DRAFT_B_ID}/export`,
    );
    expect(exportCalls[0][1].params).toEqual({ format: "xlsx" });
    expect(String(exportCalls[0][0])).not.toContain(DRAFT_A_ID);
  });
});
