/** 到货跟踪结果卡：主指标（订单到货率）+ 量级率参考 + 双 Tab 明细 + 脏数据标注。 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { describe, expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n/i18n";
import DeliveryTrackingResultCard, {
  parseDeliveryTrackingStep,
} from "../components/copilot/DeliveryTrackingResultCard";
import type { DeliveryTrackingResult } from "../components/copilot/DeliveryTrackingResultCard";
import type { CopilotStepPayload } from "../api/procurementCopilot";

function step(toolName: string, outputText: string | null): CopilotStepPayload {
  return {
    stepNo: 3,
    iterationNo: 2,
    kind: "TOOL",
    status: "SUCCEEDED",
    action: null,
    planSummary: null,
    toolName,
    answer: null,
    errorCode: null,
    errorMessage: null,
    output: outputText === null ? null : { text: outputText, truncated: false },
  };
}

const SAMPLE = JSON.stringify({
  data: {
    via: "business_db",
    datasourceId: 1,
    dataLayer: "thbi_ods_erp",
    window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
    summary: {
      totalLines: 19908, totalOrders: 4844,
      orderedQty: 98495265.255, receivedQty: 39904579.255,
      orderArrivalRate: 82.29, qtyArrivalRate: 40.51,
      allArrivedOrders: 3986, partialArrivedOrders: 448, notArrivedOrders: 410,
      excludedRows: 0,
    },
    bySupplier: [
      {
        supplierCode: "B125", supplierName: "浙江力航汽车部件有限公司",
        totalOrders: 29, allArrivedOrders: 1, partialArrivedOrders: 16,
        notArrivedOrders: 12, orderArrivalRate: 3.45,
        orderedQty: 15155100, receivedQty: 2176026, qtyArrivalRate: 14.36,
      },
    ],
    byOrder: [
      {
        poNo: "T22611POH0223", supplierCode: "B125",
        supplierName: "浙江力航汽车部件有限公司",
        orderDate: "2026-10-28", status: "partial",
        totalLines: 1, arrivedLines: 0, notArrivedLines: 1,
        orderedQty: 1047000, receivedQty: 118261, hasDirtyLines: false,
        lines: [
          {
            poLine: 3000, materialCode: "5A2100000002",
            materialDesc: "kun10/kun30/N1/N2方形焊接螺母M6",
            orderedQty: 1047000, receivedQty: 118261, openQty: 928739,
            expectedDate: "2026-11-07", lastReceiptDate: "2026-11-24",
            status: "partial", isDirty: false,
          },
        ],
      },
      {
        poNo: "T22611POH0300", supplierCode: "B125",
        supplierName: "浙江力航汽车部件有限公司",
        orderDate: "2026-10-20", status: "all",
        totalLines: 1, arrivedLines: 1, notArrivedLines: 0,
        orderedQty: 5000, receivedQty: 5000, hasDirtyLines: false,
        lines: [
          {
            poLine: 1000, materialCode: "M-ALL-1", materialDesc: "已到货物料",
            orderedQty: 5000, receivedQty: 5000, openQty: 0,
            expectedDate: "2026-10-25", lastReceiptDate: "2026-10-24",
            status: "all", isDirty: false,
          },
        ],
      },
    ],
  },
  answer: "",
});

/** 含脏数据：summary 剔除 7 行；订单 hasDirtyLines；其中一行 isDirty。 */
const DIRTY_SAMPLE = JSON.stringify({
  data: {
    via: "business_db",
    datasourceId: 1,
    dataLayer: "thbi_ods_erp",
    window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
    summary: {
      totalLines: 2, totalOrders: 1,
      orderedQty: 1500000, receivedQty: 0,
      orderArrivalRate: 0, qtyArrivalRate: 0,
      allArrivedOrders: 0, partialArrivedOrders: 0, notArrivedOrders: 0,
      excludedRows: 7,
    },
    bySupplier: [],
    byOrder: [
      {
        poNo: "T-DIRTY-1", supplierCode: "B999", supplierName: "脏数据测试供应商",
        orderDate: "2026-11-01", status: "partial",
        totalLines: 1, arrivedLines: 0, notArrivedLines: 1,
        orderedQty: 0, receivedQty: 0, hasDirtyLines: true,
        lines: [
          {
            poLine: 1000, materialCode: "DIRTY-CODE-1", materialDesc: "脏数据行",
            orderedQty: 1500000, receivedQty: 0, openQty: 1500000,
            expectedDate: "2026-11-10", lastReceiptDate: null,
            status: "notArrived", isDirty: true,
          },
          {
            poLine: 2000, materialCode: "CLEAN-CODE-1", materialDesc: "正常行",
            orderedQty: 100, receivedQty: 100, openQty: 0,
            expectedDate: "2026-11-10", lastReceiptDate: "2026-11-09",
            status: "all", isDirty: false,
          },
        ],
      },
    ],
  },
  answer: "",
});

function renderCard(result: DeliveryTrackingResult) {
  return render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <ConfigProvider locale={zhCN}>
          <DeliveryTrackingResultCard result={result} />
        </ConfigProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

describe("parseDeliveryTrackingStep", () => {
  it("解析 delivery_tracking_query 的 observation", () => {
    const parsed = parseDeliveryTrackingStep(step("delivery_tracking_query", SAMPLE));
    expect(parsed).not.toBeNull();
    expect(parsed?.summary.orderArrivalRate).toBe(82.29);
    expect(parsed?.bySupplier[0].supplierCode).toBe("B125");
    expect(parsed?.byOrder[0].status).toBe("partial");
    // 新增契约字段（任务 4 修复）也必须解析出来
    expect(parsed?.dataLayer).toBe("thbi_ods_erp");
    expect(parsed?.byOrder[0].hasDirtyLines).toBe(false);
    expect(parsed?.byOrder[0].lines[0].isDirty).toBe(false);
  });

  it("非本工具 / 坏 JSON / null ⇒ null（回退纯文本）", () => {
    expect(parseDeliveryTrackingStep(step("pending_delivery_query", SAMPLE))).toBeNull();
    expect(parseDeliveryTrackingStep(step("delivery_tracking_query", "not-json"))).toBeNull();
    expect(parseDeliveryTrackingStep(step("delivery_tracking_query", null))).toBeNull();
    expect(
      parseDeliveryTrackingStep(step("delivery_tracking_query", JSON.stringify({ data: {} }))),
    ).toBeNull();
  });
});

describe("DeliveryTrackingResultCard", () => {
  it("主指标=订单到货率；量级率为参考；数据层角标；双 Tab 明细", () => {
    const parsed = parseDeliveryTrackingStep(step("delivery_tracking_query", SAMPLE))!;
    renderCard(parsed);

    const primary = screen.getByTestId("delivery-primary-metric");
    expect(within(primary).getByText(/订单到货率/)).toBeTruthy();
    expect(within(primary).getByText("82.29%")).toBeTruthy();
    expect(within(primary).getByText(/量级加权/)).toBeTruthy();
    expect(within(primary).getByText("40.51%")).toBeTruthy();
    // 数据层角标（避免与「待到货」场景误读）
    expect(screen.getByText("数仓层 ODS_ERP")).toBeTruthy();

    const supplierTable = screen.getByTestId("delivery-supplier-table");
    expect(within(supplierTable).getByText("浙江力航汽车部件有限公司")).toBeTruthy();
    expect(within(supplierTable).getByText("B125")).toBeTruthy();
    expect(within(supplierTable).getByText("3.45%")).toBeTruthy();

    // 双 Tab，且未到货订单在前（默认激活）
    const tabs = screen.getAllByRole("tab").map((el) => el.textContent ?? "");
    expect(tabs[0]).toMatch(/未到货订单/);
    expect(tabs[1]).toMatch(/已到货订单/);
    expect(screen.getByText("T22611POH0223")).toBeTruthy();
  });

  it("脏数据：剔除 Tag + isDirty 行有「不参与统计」标识", async () => {
    const parsed = parseDeliveryTrackingStep(step("delivery_tracking_query", DIRTY_SAMPLE))!;
    // 解析层先确认两个新字段真的被读出来
    expect(parsed.summary.excludedRows).toBe(7);
    expect(parsed.byOrder[0].hasDirtyLines).toBe(true);
    expect(parsed.byOrder[0].lines[0].isDirty).toBe(true);

    const user = userEvent.setup();
    renderCard(parsed);

    const excludedTag = screen.getByText(/脏数据剔除/);
    expect(excludedTag.textContent).toContain("7");

    await user.click(screen.getByText(/物料明细/));

    const dirtyRow = screen.getByText("DIRTY-CODE-1").closest("tr")!;
    expect(within(dirtyRow).getByText(/不参与统计/)).toBeTruthy();

    const cleanRow = screen.getByText("CLEAN-CODE-1").closest("tr")!;
    expect(within(cleanRow).queryByText(/不参与统计/)).toBeNull();
  });

  it("hasDirtyLines 为 true 的订单显示「含脏数据行」提示", () => {
    const parsed = parseDeliveryTrackingStep(step("delivery_tracking_query", DIRTY_SAMPLE))!;
    renderCard(parsed);

    const orderRow = screen.getByText("T-DIRTY-1").closest("tr")!;
    expect(within(orderRow).getByText("含脏数据行")).toBeTruthy();
  });

  it("ordersTruncated：明细样本不当全量计数，tab 标题与提示都标注截断", () => {
    const truncated = JSON.parse(SAMPLE) as { data: Record<string, unknown> };
    truncated.data.ordersTruncated = true;
    const parsed = parseDeliveryTrackingStep(
      step("delivery_tracking_query", JSON.stringify(truncated)),
    )!;
    renderCard(parsed);

    expect(screen.getByTestId("delivery-orders-truncated").textContent).toContain("已截断");
    const tabs = screen.getAllByRole("tab").map((el) => el.textContent ?? "");
    expect(tabs[0]).toContain("已截断");
    expect(tabs[0]).toContain("TOP");
  });

  it("dirtyOnly 订单单独成组，不混进「未到货订单」计数", () => {
    const dirtyOnly = JSON.parse(SAMPLE) as { data: { byOrder: unknown[] } };
    dirtyOnly.data.byOrder = [
      {
        poNo: "T-DIRTY-ONLY", supplierCode: "B999", supplierName: null,
        orderDate: "2026-11-01", status: "dirtyOnly",
        totalLines: 0, arrivedLines: 0, notArrivedLines: 0,
        orderedQty: 0, receivedQty: 0, hasDirtyLines: true,
        lines: [],
      },
    ];
    const parsed = parseDeliveryTrackingStep(
      step("delivery_tracking_query", JSON.stringify(dirtyOnly)),
    )!;
    renderCard(parsed);

    const tabs = screen.getAllByRole("tab").map((el) => el.textContent ?? "");
    expect(tabs.some((label) => label.includes("数据异常订单（1）"))).toBe(true);
    expect(tabs[0]).toContain("未到货订单（0）");
  });

  it("主次关系：订单到货率是大号主指标，量级率小号且带口径说明", () => {
    const parsed = parseDeliveryTrackingStep(step("delivery_tracking_query", SAMPLE))!;
    renderCard(parsed);

    const primaryRate = screen.getByTestId("delivery-primary-rate");
    expect(primaryRate.textContent).toBe("82.29%");
    const primaryFontSize = Number.parseFloat(primaryRate.style.fontSize);
    expect(primaryFontSize).toBeGreaterThanOrEqual(20);

    const qtyReference = screen.getByTestId("delivery-qty-rate-reference");
    expect(qtyReference.textContent).toContain("量级加权");
    expect(qtyReference.textContent).toContain("40.51%");
    expect(qtyReference.textContent).toContain("受超大额未到货行影响");
    expect(qtyReference.textContent).toContain("仅作参考");
    expect(Number.parseFloat(qtyReference.style.fontSize)).toBeLessThan(primaryFontSize);
  });
});
