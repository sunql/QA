/** Task 8：报告执行证据面板（EvidencePanel）+ parseReportPayload.evidence 收窄。
 *
 * 契约（对齐 backend report_planner.py 的 payload.evidence 形状，camelCase）：
 * - evidence = { stepResults: [{ index, type, sql, status, error, summary, schemaVersion }] }
 * - 每步渲染「步骤 N：type」/ 状态标签 / SQL（<pre>）/ 摘要；
 * - evidence 缺省 / stepResults 为空时**不渲染**（安全，旧 payload 不破）。
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
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
