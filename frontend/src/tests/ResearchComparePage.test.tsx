// ResearchComparePage 页面级测试（feat-research-entry Task 12）。
//
// 覆盖 ?ids= 解析（trim + 去空 + 有效列表）与空/缺省回退。
// Task 10 教训：缺陷藏在无测试的页面里 —— 本页必须有测试。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { MemoryRouter } from "react-router-dom";
import ResearchComparePage from "../pages/research/ResearchComparePage";
import { listReports } from "../api/research";

vi.mock("../api/research", () => ({
  listReports: vi.fn(),
  getReport: vi.fn(),
}));

const listReportsMock = vi.mocked(listReports);

function renderPage(initialPath: string) {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={[initialPath]}>
        <ResearchComparePage />
      </MemoryRouter>
    </ConfigProvider>,
  );
}

describe("ResearchComparePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listReportsMock.mockResolvedValue([]);
  });

  it("解析 ?ids 列表（trim + 去空）并下传给对比视图", async () => {
    // %2C=, %20=空格 ⇒ "s1, s2,, s3," → ["s1", "s2", "s3"]
    renderPage("/research/compare?ids=s1%2C%20s2%2C%2C%20s3%2C");

    await waitFor(() => {
      expect(listReportsMock).toHaveBeenCalledWith("s1");
      expect(listReportsMock).toHaveBeenCalledWith("s2");
      expect(listReportsMock).toHaveBeenCalledWith("s3");
    });
    expect(listReportsMock).toHaveBeenCalledTimes(3);
  });

  it("缺省 / 空 ids 渲染空态文案且不请求报告", () => {
    renderPage("/research/compare");

    expect(screen.getByText("暂无可对比的报告")).toBeInTheDocument();
    expect(listReportsMock).not.toHaveBeenCalled();
  });
});
