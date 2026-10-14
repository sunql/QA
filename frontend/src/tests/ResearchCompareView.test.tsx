// ResearchCompareView 单元测试（feat-research-entry Task 12，TDD RED→GREEN）。
//
// 覆盖：
// 1. 并排结构：4 行标签（标题/执行摘要/关键发现/方法学）对齐各 session 列。
// 2. 「最新 published」解析：version 最大且 status=published 的版本才被拉取。
// 3. 无 published 报告的 session 显示「未生成报告」占位，不破坏对齐。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { ResearchCompareView } from "../components/research/ResearchCompareView";
import { listReports, getReport } from "../api/research";
import type { ResearchReport, ResearchReportSummary } from "../types/research";

vi.mock("../api/research", () => ({
  listReports: vi.fn(),
  getReport: vi.fn(),
}));

const listReportsMock = vi.mocked(listReports);
const getReportMock = vi.mocked(getReport);

const CREATED_AT = "2026-01-01T00:00:00Z";

function summary(
  version: number,
  status: "published" | "superseded",
): ResearchReportSummary {
  return { id: `r-${version}`, version, status, createdAt: CREATED_AT };
}

function makeReport(sessionId: string, version: number, title: string): ResearchReport {
  return {
    id: `r-${sessionId}-${version}`,
    version,
    status: "published",
    payload: {
      sessionId,
      turnId: "t1",
      mode: "research",
      title,
      question: `${title}的问题`,
      sections: [
        {
          id: "executive_summary",
          kind: "executive_summary",
          title: "执行摘要",
          blocks: [{ type: "text", content: `${title}的摘要` }],
        },
        {
          id: "methodology",
          kind: "methodology",
          title: "方法学",
          blocks: [{ type: "text", content: `${title}的方法学` }],
        },
      ],
      findingsRef: [
        { findingId: "f1", claim: `${title}的结论`, confidence: 0.9, verified: true },
      ],
    },
    renderedMd: "# 报告",
    createdAt: CREATED_AT,
  };
}

function renderView(sessionIds: string[]) {
  return render(
    <ConfigProvider locale={zhCN}>
      <ResearchCompareView sessionIds={sessionIds} />
    </ConfigProvider>,
  );
}

describe("ResearchCompareView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("并排渲染四行结构：2 个有报告 + 1 个无报告", async () => {
    listReportsMock.mockImplementation(async (sessionId: string) => {
      if (sessionId === "s1") {
        return [summary(1, "published"), summary(3, "published"), summary(2, "superseded")];
      }
      if (sessionId === "s2") {
        return [summary(1, "published")];
      }
      return []; // s3 无报告
    });
    getReportMock.mockImplementation(async (sessionId: string, version?: number) => {
      return makeReport(sessionId, version ?? 0, sessionId === "s1" ? "报告甲" : "报告乙");
    });

    const { container } = renderView(["s1", "s2", "s3"]);

    // 四行标签
    await waitFor(() => {
      expect(screen.getByText("标题")).toBeInTheDocument();
      expect(screen.getByText("执行摘要")).toBeInTheDocument();
      expect(screen.getByText("关键发现")).toBeInTheDocument();
      expect(screen.getByText("方法学")).toBeInTheDocument();
    });

    // 两个有报告 session 的标题与内容
    expect(screen.getByText("报告甲")).toBeInTheDocument();
    expect(screen.getByText("报告乙")).toBeInTheDocument();
    expect(screen.getByText("报告甲的摘要")).toBeInTheDocument();
    expect(screen.getByText("报告甲的结论（置信度 0.9）")).toBeInTheDocument();
    expect(screen.getByText("报告甲的方法学")).toBeInTheDocument();

    // 无报告 session：四行都显示占位，不破坏对齐
    expect(screen.getAllByText("未生成报告")).toHaveLength(4);

    // 对齐结构：4 个 tbody 行，每行 = 1 标签 + 3 个 session 单元格
    const rows = container.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(4);
    for (const row of Array.from(rows)) {
      expect(row.querySelectorAll("th, td")).toHaveLength(4);
    }
  });

  it("「最新 published」取 version 最大的 published 版本", async () => {
    listReportsMock.mockImplementation(async (sessionId: string) => {
      if (sessionId === "s1") {
        return [summary(1, "published"), summary(3, "published"), summary(2, "superseded")];
      }
      return [];
    });
    getReportMock.mockResolvedValue(makeReport("s1", 3, "报告甲"));

    renderView(["s1"]);

    await waitFor(() => expect(getReportMock).toHaveBeenCalledWith("s1", 3));
    expect(getReportMock).not.toHaveBeenCalledWith("s1", 1);
    expect(getReportMock).not.toHaveBeenCalledWith("s1", 2);
  });
});
