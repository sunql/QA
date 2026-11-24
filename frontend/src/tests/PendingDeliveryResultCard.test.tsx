/** 待到货结构化结果卡：数据不经 LLM 复述，直接渲染工具 outputJson。 */

import { render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { describe, expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "../i18n/i18n";
vi.mock("../utils/download", () => ({ downloadCsv: vi.fn() }));

import { downloadCsv } from "../utils/download";
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
      { supplierCode: "J1", supplierName: "河北新泰鸿汽车零部件有限公司",
        openLines: 2405, openQty: 57363604, overdueLines: 2405 },
    ],
    topLines: [
      {
        poNo: "T22511POH1251Q", poLine: 1000, materialCode: "502447700001",
        supplierCode: "F109", supplierName: "上海纳特汽车标准件有限公司",
        orderedQty: 1000000, receivedQty: 9600,
        openQty: 990400, expectedDate: "2025-11-28 00:00:00", overdueDays: 360,
      },
    ],
  },
  answer: "文字建议",
});

import userEvent from "@testing-library/user-event";

function renderCardWithUser(outputText: string | null) {
  const user = userEvent.setup();
  const utils = render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <PendingDeliveryResultCard result={parsePendingDeliveryStep(stepWith(outputText))!} />
      </ConfigProvider>
    </I18nextProvider>,
  );
  return { user, ...utils };
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
  it("概览默认展开；供应商/TOP 面板收起，点击展开后见名称与行", async () => {
    const { user } = renderCardWithUser(SAMPLE);
    expect(screen.getByText("结果概览")).toBeTruthy();
    expect(document.body.textContent).toContain("209,421,924.058");

    // 收起态：供应商表与 TOP 行都不在 DOM（antd Collapse 惰性渲染）
    expect(screen.queryByText("T22511POH1251Q")).toBeNull();
    expect(screen.queryByText(/河北新泰鸿/)).toBeNull();

    // 展开供应商汇总
    await user.click(screen.getByText(/按供应商汇总/));
    await waitFor(() =>
      expect(screen.getByText("河北新泰鸿汽车零部件有限公司（J1）")).toBeTruthy(),
    );

    // 展开 TOP 行
    await user.click(screen.getByText(/TOP 欠交订单行/));
    await waitFor(() => expect(screen.getByText("T22511POH1251Q")).toBeTruthy());
    expect(screen.getByText("上海纳特汽车标准件有限公司（F109）")).toBeTruthy();
    // 逾期 360 天 > 90 ⇒ Tag 形式
    expect(screen.getByText("360")).toBeTruthy();
  });

  it("空结果也渲染（openLines=0）", async () => {
    const { user } = renderCardWithUser(
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
    await user.click(screen.getByText(/按供应商汇总/));
    expect(document.body.textContent).toContain("共 0 家");
  });
});

describe("导出明细(CSV)", () => {
  it("点击导出：明细行 + 供应商汇总段写入 CSV", async () => {
    const { user } = renderCardWithUser(SAMPLE);
    await user.click(screen.getByRole("button", { name: /导出明细/ }));

    expect(downloadCsv).toHaveBeenCalledTimes(1);
    const [rows, filename] = (downloadCsv as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(filename).toContain("待到货明细_");
    expect(rows).toHaveLength(2);  // 1 明细 + 1 供应商汇总
    expect(rows[0]["订单号"]).toBe("T22511POH1251Q");
    expect(rows[0]["供应商名称"]).toBe("上海纳特汽车标准件有限公司");
    expect(rows[1]["订单号"]).toBe("[供应商汇总] 河北新泰鸿汽车零部件有限公司");
    expect(rows[1]["欠交量"]).toBe(57363604);
  });
});
