/** 待到货结构化结果卡：数据不经 LLM 复述，直接渲染工具 outputJson。 */

import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { describe, expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "../i18n/i18n";
import PendingDeliveryResultCard, {
  parsePendingDeliveryStep,
} from "../components/copilot/PendingDeliveryResultCard";
import type { CopilotStepPayload } from "../api/procurementCopilot";

function stepWith(outputText: string | null): CopilotStepPayload {
  return {
    stepNo: 1,
    iterationNo: 1,
    kind: "TOOL",
    status: "SUCCEEDED",
    action: null,
    planSummary: null,
    toolName: "pending_delivery_query",
    answer: null,
    errorCode: null,
    errorMessage: null,
    output: outputText === null ? null : { text: outputText, truncated: false },
  };
}

const SAMPLE = JSON.stringify({
  data: {
    summary: { openLines: 9828, openQty: 209421924.058, overdueLines: 9828, excludedRows: 7 },
    bySupplier: [
      { supplierCode: "J1", openLines: 2405, openQty: 57363604, overdueLines: 2405 },
    ],
    topLines: [
      {
        poNo: "T22511POH1251Q", poLine: 1000, materialCode: "502447700001",
        supplierCode: "F109", orderedQty: 1000000, receivedQty: 9600,
        openQty: 990400, expectedDate: "2025-11-28 00:00:00", overdueDays: 360,
      },
    ],
  },
  answer: "文字建议",
});

function renderCard(outputText: string | null) {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <PendingDeliveryResultCard result={parsePendingDeliveryStep(stepWith(outputText))!} />
      </ConfigProvider>
    </I18nextProvider>,
  );
}

describe("parsePendingDeliveryStep", () => {
  it("SUCCEEDED + 合法 JSON ⇒ 解析出 summary/bySupplier/topLines", () => {
    const parsed = parsePendingDeliveryStep(stepWith(SAMPLE));
    expect(parsed?.summary.openLines).toBe(9828);
    expect(parsed?.bySupplier[0].supplierCode).toBe("J1");
    expect(parsed?.topLines[0].overdueDays).toBe(360);
  });

  it("非本工具 / 非 SUCCEEDED / 坏 JSON / 缺字段 ⇒ null（回退纯文本）", () => {
    const base = stepWith(SAMPLE);
    expect(parsePendingDeliveryStep({ ...base, toolName: "material_search" })).toBeNull();
    expect(parsePendingDeliveryStep({ ...base, status: "FAILED" })).toBeNull();
    expect(parsePendingDeliveryStep(stepWith("not-json"))).toBeNull();
    expect(parsePendingDeliveryStep(stepWith(JSON.stringify({ data: {} })))).toBeNull();
    expect(parsePendingDeliveryStep(stepWith(null))).toBeNull();
  });
});

describe("PendingDeliveryResultCard", () => {
  it("统计块 + 供应商表 + TOP 行表，逾期 >90 天标红", () => {
    renderCard(SAMPLE);
    expect(screen.getAllByText("9,828").length).toBeGreaterThan(0);
    expect(screen.getByText("57,363,604")).toBeTruthy();
    expect(screen.getByText("990,400")).toBeTruthy();
    // Statistic 把整数/小数拆在不同 span ⇒ 用 textContent 断言
    expect(document.body.textContent).toContain("209,421,924.058");
    expect(screen.getByText("J1")).toBeTruthy();
    expect(screen.getByText("T22511POH1251Q")).toBeTruthy();
    // 逾期 360 天 > 90 ⇒ Tag 形式
    expect(screen.getByText("360")).toBeTruthy();
  });

  it("空结果也渲染（openLines=0）", () => {
    renderCard(
      JSON.stringify({
        data: {
          summary: { openLines: 0, openQty: 0, overdueLines: 0, excludedRows: 0 },
          bySupplier: [],
          topLines: [],
        },
        answer: "",
      }),
    );
    expect(screen.getByText("待到货分析结果")).toBeTruthy();
  });
});
