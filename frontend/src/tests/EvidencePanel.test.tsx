/** Task 8：报告执行证据面板（EvidencePanel）+ parseReportPayload.evidence 收窄。
 *
 * 契约（对齐 backend report_planner.py 的 payload.evidence 形状，camelCase）：
 * - evidence = { stepResults: [{ index, type, sql, status, error, summary, schemaVersion }] }
 * - 每步渲染「步骤 N：type」/ 状态标签 / SQL（<pre>）/ 摘要；
 * - evidence 缺省 / stepResults 为空时**不渲染**（安全，旧 payload 不破）。
 */
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { ConfigProvider } from "antd";

import { EvidencePanel } from "../components/research/EvidencePanel";
import { parseReportPayload } from "../components/research/ReportRenderer";
import type { ReportEvidence } from "../components/research/ReportRenderer";

function makeEvidence(): ReportEvidence {
  return {
    stepResults: [
      {
        index: 0,
        type: "成功步",
        sql: "SELECT 1 AS amount FROM DUAL",
        status: "success",
        error: null,
        summary: { total: "100", rowCount: 1 },
        schemaVersion: 1,
      },
      {
        index: 1,
        type: "失败步",
        sql: null,
        status: "failed",
        error: "守卫拒绝",
        summary: null,
        schemaVersion: 1,
      },
    ],
  };
}

/** Task 14：带结构化 summary（真实线上形状，camelCase）的单步证据。 */
function makeStructuredEvidence(): ReportEvidence {
  return {
    stepResults: [
      {
        index: 0,
        type: "聚合步",
        sql: "SELECT bpsnum_0, qty FROM dual",
        status: "success",
        error: null,
        summary: {
          total: "100",
          baseline: "80",
          current: "100",
          delta: "20",
          deltaRatio: "0.25",
          topN: [
            { rank: 1, row: { BPSNUM_0: "SUP-A", QTY: "12.5" } },
            { rank: 2, row: { BPSNUM_0: "SUP-B", QTY: "3" } },
          ],
          sampleRows: [{ BPSNUM_0: "RAW-LEAK", QTY: "999" }],
          timeRange: { start: "2024-01", end: "2024-06" },
          empty: false,
        },
        schemaVersion: 1,
      },
    ],
  };
}

describe("EvidencePanel", () => {
  it("按步骤渲染 步骤标签 / 状态 / SQL / 摘要", () => {
    render(
      <ConfigProvider>
        <EvidencePanel evidence={makeEvidence()} />
      </ConfigProvider>
    );
    expect(screen.getByTestId("evidence-panel")).toBeInTheDocument();
    expect(screen.getAllByTestId("evidence-step")).toHaveLength(2);

    // 步骤 N：type
    expect(screen.getByText("步骤 0：成功步")).toBeInTheDocument();
    expect(screen.getByText("步骤 1：失败步")).toBeInTheDocument();

    // 状态标签
    expect(screen.getByText("成功")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();

    // SQL（<pre>）
    const sql = screen.getByText("SELECT 1 AS amount FROM DUAL");
    expect(sql.tagName).toBe("PRE");

    // 摘要（标量键值）
    expect(screen.getByText(/total: 100/)).toBeInTheDocument();

    // 失败步的错误信息
    expect(screen.getByText("守卫拒绝")).toBeInTheDocument();
  });

  it("summary.topN ⇒ 渲染紧凑表（rank + 键并集）并如实标注为按度量排名", () => {
    render(
      <ConfigProvider>
        <EvidencePanel evidence={makeStructuredEvidence()} />
      </ConfigProvider>
    );
    const table = screen.getByTestId("evidence-topn");
    // 表头 = rank + 各行键并集
    expect(within(table).getByText("rank")).toBeInTheDocument();
    expect(within(table).getByText("BPSNUM_0")).toBeInTheDocument();
    expect(within(table).getByText("QTY")).toBeInTheDocument();
    // 值按原样渲染
    expect(within(table).getByText("SUP-A")).toBeInTheDocument();
    expect(within(table).getByText("SUP-B")).toBeInTheDocument();
    expect(within(table).getByText("12.5")).toBeInTheDocument();
    // 排名标注（非采样、非原图）
    expect(screen.getByText(/结果行按度量排名/)).toBeInTheDocument();
  });

  it("summary.timeRange ⇒ 渲染一行时间范围", () => {
    render(
      <ConfigProvider>
        <EvidencePanel evidence={makeStructuredEvidence()} />
      </ConfigProvider>
    );
    expect(screen.getByTestId("evidence-time-range")).toBeInTheDocument();
    expect(screen.getByText("时间范围 2024-01 – 2024-06")).toBeInTheDocument();
  });

  it("summary.sampleRows 不铺开（体积/噪声）", () => {
    render(
      <ConfigProvider>
        <EvidencePanel evidence={makeStructuredEvidence()} />
      </ConfigProvider>
    );
    expect(screen.queryByText(/RAW-LEAK/)).toBeNull();
  });

  it("仅标量 summary ⇒ 不渲染 topN 表 / 时间范围（不误渲染空白）", () => {
    render(
      <ConfigProvider>
        <EvidencePanel evidence={makeEvidence()} />
      </ConfigProvider>
    );
    expect(screen.queryByTestId("evidence-topn")).toBeNull();
    expect(screen.queryByTestId("evidence-time-range")).toBeNull();
  });

  it("标量 baseline/current/delta/deltaRatio 仍渲染在 <ul>（不回归）", () => {
    render(
      <ConfigProvider>
        <EvidencePanel evidence={makeStructuredEvidence()} />
      </ConfigProvider>
    );
    expect(screen.getByText(/baseline: 80/)).toBeInTheDocument();
    expect(screen.getByText(/current: 100/)).toBeInTheDocument();
    expect(screen.getByText(/delta: 20/)).toBeInTheDocument();
    expect(screen.getByText(/deltaRatio: 0.25/)).toBeInTheDocument();
  });

  it("evidence 缺省时不渲染（安全）", () => {
    const { container } = render(
      <ConfigProvider>
        <EvidencePanel />
      </ConfigProvider>
    );
    expect(container.querySelector("[data-testid='evidence-panel']")).toBeNull();
  });

  it("stepResults 为空时不渲染（安全）", () => {
    const { container } = render(
      <ConfigProvider>
        <EvidencePanel evidence={{ stepResults: [] }} />
      </ConfigProvider>
    );
    expect(container.querySelector("[data-testid='evidence-panel']")).toBeNull();
  });
});

describe("parseReportPayload.evidence", () => {
  it("解析 evidence.stepResults", () => {
    const payload = parseReportPayload({
      sessionId: "s-1",
      turnId: "t-1",
      evidence: {
        stepResults: [
          {
            index: 0,
            type: "sql",
            sql: "SELECT 1 FROM DUAL",
            status: "success",
            error: null,
            summary: { total: "1" },
            schemaVersion: 1,
          },
        ],
      },
    });
    expect(payload.evidence?.stepResults).toHaveLength(1);
    expect(payload.evidence?.stepResults[0].sql).toBe("SELECT 1 FROM DUAL");
    expect(payload.evidence?.stepResults[0].status).toBe("success");
  });

  it("缺 evidence 时为 undefined（不炸）", () => {
    const payload = parseReportPayload({ sessionId: "s-1", turnId: "t-1" });
    expect(payload.evidence).toBeUndefined();
  });

  it("evidence 字段非法时安全降级", () => {
    const payload = parseReportPayload({
      sessionId: "s-1",
      turnId: "t-1",
      evidence: { stepResults: "not-an-array" },
    });
    expect(payload.evidence?.stepResults).toEqual([]);
  });
});
