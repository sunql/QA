/** ReportRenderer 单元测试（feat-research-entry Task 10，TDD RED→GREEN）。
 *
 * 契约（对齐 backend report_planner.py 的 JSON 形状，camelCase）：
 * - payload = { sessionId, turnId, mode, title, question, sections[], findingsRef[] }
 * - section = { id, kind, title, blocks[] }
 * - block = { type: "text"|"chart"|"table"|"bullet_list", content, sourceRefs[] }
 * - text → 段落；table → 表格；chart → 表格兜底（v1 chartType=null）；bullet_list → ul/li
 * - 块按 sections[].blocks[] 顺序渲染，每个块带 data-block-type 供顺序断言
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { ReportRenderer } from "../components/research/ReportRenderer";
import type { ReportPayload } from "../components/research/ReportRenderer";

function makePayload(): ReportPayload {
  return {
    sessionId: "s-1",
    turnId: "t-1",
    mode: "research",
    title: "本月收货量分析报告",
    question: "本月收货量趋势如何？",
    sections: [
      {
        id: "summary",
        kind: "summary",
        title: "执行摘要",
        blocks: [
          { type: "text", content: "收货量整体环比上升。", sourceRefs: [] },
          { type: "bullet_list", content: ["指标 A 上升", "指标 B 持平"], sourceRefs: [] },
          {
            type: "table",
            content: {
              columns: ["月份", "收货量"],
              rows: [{ 月份: "8月", 收货量: 120 }, { 月份: "9月", 收货量: 150 }],
            },
            sourceRefs: [],
          },
        ],
      },
      {
        id: "detail",
        kind: "detail",
        title: "明细",
        blocks: [
          {
            type: "chart",
            content: {
              title: "收货量月度趋势",
              chartType: null,
              columns: ["月份", "收货量"],
              rows: [{ 月份: "10月", 收货量: 180 }],
            },
            sourceRefs: [],
          },
        ],
      },
    ],
    findingsRef: [{ findingId: "f-1", claim: "收货量上升", confidence: 0.9, verified: true }],
  };
}

describe("ReportRenderer", () => {
  it("渲染报告标题与原始问题", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    expect(screen.getByText("本月收货量分析报告")).toBeInTheDocument();
    expect(screen.getByText("本月收货量趋势如何？")).toBeInTheDocument();
  });

  it("渲染 section 标题", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    expect(screen.getByText("执行摘要")).toBeInTheDocument();
    expect(screen.getByText("明细")).toBeInTheDocument();
  });

  it("text 块渲染为段落", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    expect(screen.getByText("收货量整体环比上升。")).toBeInTheDocument();
  });

  it("bullet_list 块渲染为无序列表", () => {
    const { container } = render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    const items = container.querySelectorAll("li");
    const texts = Array.from(items).map((li) => li.textContent);
    expect(texts).toContain("指标 A 上升");
    expect(texts).toContain("指标 B 持平");
  });

  it("table 块渲染表头与数据行", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    expect(screen.getByText("8月")).toBeInTheDocument();
    expect(screen.getByText("150")).toBeInTheDocument();
  });

  it("chart 块以表格兜底渲染（v1 chartType=null）", () => {
    render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    // chart 块的列头（月份）在表格兜底中渲染
    expect(screen.getByText("收货量月度趋势")).toBeInTheDocument();
    const header = screen.getAllByText("月份");
    expect(header.length).toBeGreaterThanOrEqual(2); // table + chart 兜底各一列
  });

  it("按 sections[].blocks[] 顺序渲染，data-block-type 序列正确", () => {
    const { container } = render(
      <ConfigProvider>
        <ReportRenderer payload={makePayload()} />
      </ConfigProvider>
    );
    const types = Array.from(container.querySelectorAll("[data-testid='report-block']")).map(
      (el) => el.getAttribute("data-block-type"),
    );
    expect(types).toEqual(["text", "bullet_list", "table", "chart"]);
  });
});
