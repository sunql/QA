/** 到货跟踪欢迎态「本期业务现状」卡组件测试。
 *
 * 与 `PendingDeliveryWelcome` 同构：进入场景即出数（零 LLM，后端只读 SQL）。
 * 口径 = 本期（企业 24 日结算周期），与场景 chip「本期到货概览（24 日周期）」同源。
 */

import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it } from "vitest";
import i18n from "../i18n/i18n";
import DeliveryTrackingWelcome from "../components/copilot/DeliveryTrackingWelcome";
import type { DeliveryTrackingOverviewResponse } from "../api/procurementCopilot";

const OVERVIEW: DeliveryTrackingOverviewResponse = {
  window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
  summary: {
    totalLines: 19902,
    totalOrders: 4843,
    orderedQty: 88619265.0,
    receivedQty: 41414210.0,
    orderArrivalRate: 83.67,
    qtyArrivalRate: 46.73,
    allArrivedOrders: 4052,
    partialArrivedOrders: 434,
    notArrivedOrders: 357,
    excludedRows: 6,
  },
  bySupplier: [
    {
      supplierCode: "B125",
      supplierName: "浙江力航汽车部件有限公司",
      totalOrders: 29,
      allArrivedOrders: 1,
      partialArrivedOrders: 16,
      notArrivedOrders: 12,
      orderArrivalRate: 3.45,
      orderedQty: 15155100.0,
      receivedQty: 2176026.0,
    },
  ],
};

function renderWelcome(
  overview: Partial<{
    loading: boolean;
    error: string | null;
    data: DeliveryTrackingOverviewResponse | null;
  }> = {},
) {
  render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <DeliveryTrackingWelcome
          overview={{ loading: false, error: null, data: OVERVIEW, ...overview }}
        />
      </ConfigProvider>
    </I18nextProvider>,
  );
}

/** antd Statistic 把数值拆成 int/decimal 两个节点 ⇒ 取 stat 卡整体文本。 */
function statisticText(title: string): string {
  const node = screen.getByText(title).closest(".ant-statistic");
  expect(node).toBeTruthy();
  return node?.textContent ?? "";
}

describe("DeliveryTrackingWelcome", () => {
  it("渲染本期业务现状卡：主指标 + 窗口 + 三态", () => {
    renderWelcome();

    expect(screen.getByText("本期业务现状（实时）")).toBeTruthy();
    // 主指标：订单到货率（业务口径）
    expect(statisticText("订单到货率")).toContain("83.67%");
    // 量级加权到货率（参考）
    expect(statisticText("量级加权到货率")).toContain("46.73%");
    expect(statisticText("订单总数")).toContain("4,843");
    expect(statisticText("未到货订单")).toContain("357");
    // 统计窗口 = 本期 24 日周期
    expect(screen.getByText(/2026-10-24 ~ 2026-11-24/)).toBeTruthy();
    // 三态计数（全到货 4052）
    expect(screen.getByText(/4052/)).toBeTruthy();
  });

  it("展示未到货最多的供应商（含名称）", () => {
    renderWelcome();

    // 「未到货最多供应商: 名称(未到货 12)」在同一个节点里 ⇒ 用子串匹配
    expect(screen.getByText(/未到货最多供应商/)).toBeTruthy();
    expect(screen.getByText(/浙江力航汽车部件有限公司/)).toBeTruthy();
  });

  it("无数据时只渲染标题，不崩", () => {
    renderWelcome({ data: null });

    expect(screen.getByText("本期业务现状（实时）")).toBeTruthy();
    expect(screen.queryByText("订单到货率")).toBeNull();
  });

  it("后端报错时显示告警，不白屏", () => {
    renderWelcome({ data: null, error: "该数据源执行到货跟踪概览失败" });

    expect(screen.getByText("该数据源执行到货跟踪概览失败")).toBeTruthy();
  });
});
