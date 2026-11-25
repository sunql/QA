/** 供应商风险速览结果卡：头部展示口径（编码+企业键+名称）+ 分析结论。 */

import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { describe, expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n/i18n";
import SupplierRiskResultCard, {
  parseSupplierRiskStep,
} from "../components/copilot/SupplierRiskResultCard";
import { parseDeliveryTrackingStep } from "../components/copilot/DeliveryTrackingResultCard";
import type { CopilotStepPayload } from "../api/procurementCopilot";

function riskStep(toolName: string, outputText: string | null): CopilotStepPayload {
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
    profile: {
      enterpriseKey: 195779358766766,
      enterpriseCode: "B125",
      supplierName: "杭州钢创物资有限公司",
      entityType: "SUPPLIER",
    },
    level: "low",
    levelSource: "supplier_risk_score_main",
    contributions: [
      {
        featureName: "SUPPLIER_OTD_3M",
        featureAlias: "供应商3月准时交付率",
        value: "100.00",
        unit: "%",
        threshold: "90",
        passed: true,
        note: "准时交付率100% 正常",
      },
      {
        featureName: "SUPPLIER_DEFECT_RATE_3M",
        featureAlias: "供应商3月来料不良率",
        value: "0.00",
        unit: "%",
        threshold: "5",
        passed: true,
      },
    ],
    riskPoints: "各项指标处于阈值范围内，风险可控。",
    riskPointsSource: "llm",
    recommendedActions: ["维持当前合作模式", "下季度例行复核"],
  },
  answer: "",
});

describe("parseSupplierRiskStep", () => {
  it("解析 supplier_risk 工具的 observation", () => {
    const parsed = parseSupplierRiskStep(riskStep("supplier_risk", SAMPLE));
    expect(parsed).not.toBeNull();
    expect(parsed?.profile.enterpriseCode).toBe("B125");
    expect(parsed?.profile.supplierName).toBe("杭州钢创物资有限公司");
    expect(parsed?.level).toBe("low");
    expect(parsed?.contributions).toHaveLength(2);
    expect(parsed?.riskPointsSource).toBe("llm");
  });

  it("非 supplier_risk 工具 / 坏 JSON ⇒ null（回退纯文本）", () => {
    expect(parseSupplierRiskStep(riskStep("pending_delivery_query", SAMPLE))).toBeNull();
    expect(parseSupplierRiskStep(riskStep("supplier_risk", "not-json"))).toBeNull();
    expect(parseSupplierRiskStep(riskStep("supplier_risk", null))).toBeNull();
  });
});

describe("渲染器分发", () => {
  it("delivery_tracking 走新卡解析器（导出面未被接线改坏）", () => {
    const parsed = parseDeliveryTrackingStep({
      stepNo: 1,
      iterationNo: 1,
      kind: "TOOL",
      status: "SUCCEEDED",
      action: null,
      planSummary: null,
      toolName: "delivery_tracking_query",
      answer: null,
      errorCode: null,
      errorMessage: null,
      output: {
        text: JSON.stringify({
          data: {
            window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
            summary: {
              totalLines: 19908, totalOrders: 4844, orderedQty: 1, receivedQty: 1,
              orderArrivalRate: 82.29, qtyArrivalRate: 40.51,
              allArrivedOrders: 3986, partialArrivedOrders: 448,
              notArrivedOrders: 410, excludedRows: 0,
            },
            bySupplier: [], byOrder: [],
          },
        }),
        truncated: false,
      },
    });
    expect(parsed).not.toBeNull();
    expect(parsed?.summary.orderArrivalRate).toBe(82.29);
    // 两个场景互不串味：待到货解析器不接 delivery_tracking 工具
    expect(parseSupplierRiskStep(riskStep("delivery_tracking_query", SAMPLE))).toBeNull();
  });
});

describe("SupplierRiskResultCard", () => {
  it("头部 = 编码（企业键）+ 名称 + 分析结论；明细/建议渲染", () => {
    const parsed = parseSupplierRiskStep(riskStep("supplier_risk", SAMPLE))!;
    render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <ConfigProvider locale={zhCN}>
            <SupplierRiskResultCard result={parsed} />
          </ConfigProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText("供应商 B125（企业键 195779358766766）")).toBeTruthy();
    expect(screen.getByText("杭州钢创物资有限公司")).toBeTruthy();
    expect(screen.getByText(/低风险/)).toBeTruthy();
    expect(screen.getByText("各项指标处于阈值范围内，风险可控。")).toBeTruthy();
    expect(screen.getByText(/来源：LLM 生成/)).toBeTruthy();
    expect(screen.getByText(/维持当前合作模式/)).toBeTruthy();
  });
});
