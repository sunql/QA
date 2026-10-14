// ResearchReportPage 页面级测试（feat-research-entry Task 15 / B3 + A1.1）。
//
// A1.1：payload 收窄必须走 ReportRenderer.parseReportPayload（唯一收窄点，
// ResearchCompareView 已如此）。本文件的两条「坏 payload」用例在迁移前应当**崩渲染**
// （payload.sections.map on undefined）——那是 RED；迁移后收窄为安全默认值即绿。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ResearchReportPage from "../pages/research/ResearchReportPage";
import { useResearchStore } from "../stores/researchStore";

const httpMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock("../api/client", () => ({ httpClient: httpMock, apiClient: httpMock }));

const GOOD_PAYLOAD = {
  sessionId: "s1", turnId: "t1", mode: "research",
  title: "本月收货量分析报告", question: "本月收货量趋势如何？",
  sections: [
    {
      id: "summary", kind: "summary", title: "执行摘要",
      blocks: [{ type: "text", content: "本月收货量环比上升 12%。", sourceRefs: [] }],
    },
  ],
  findingsRef: [],
};

function reportWith(payload: unknown, version = 1) {
  return { id: "r1", version, status: "published", payload, renderedMd: "", createdAt: "2026-01-01T00:00:00Z" };
}

const SUMMARIES = [
  { id: "r1", version: 1, status: "published", createdAt: "2026-01-01T00:00:00Z" },
  { id: "r2", version: 2, status: "draft", createdAt: "2026-01-02T00:00:00Z" },
];

// 按 URL 分发（/reports 必须先判，虽然它不以 /report 结尾）。
function mockApi(report: unknown, summaries: unknown[] = SUMMARIES) {
  httpMock.get.mockImplementation((url: string) => {
    if (url.endsWith("/reports")) return Promise.resolve({ data: summaries });
    if (url.endsWith("/report")) return Promise.resolve({ data: report });
    return Promise.resolve({ data: null });
  });
}

function renderPage(initialPath = "/research/s1/report") {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/research/:id/report" element={<ResearchReportPage />} />
          <Route path="/research/report" element={<ResearchReportPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>,
  );
}

describe("ResearchReportPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useResearchStore.getState().reset();
  });

  it("挂载即拉版本列表与报告；渲染 payload.title 与版本标签", async () => {
    mockApi(reportWith(GOOD_PAYLOAD));
    renderPage();

    await waitFor(() => {
      expect(httpMock.get).toHaveBeenCalledWith("/research/sessions/s1/reports");
      expect(httpMock.get).toHaveBeenCalledWith("/research/sessions/s1/report", { params: undefined });
    });
    // 非空 title 出现两处：页面头部 Typography.Title + ReportRenderer 的 <h2>。
    expect(await screen.findAllByText("本月收货量分析报告")).toHaveLength(2);
    expect(screen.getByText("版本 1")).toBeInTheDocument();
    expect(screen.getByText("版本 2")).toBeInTheDocument();
    expect(screen.getByText("执行摘要")).toBeInTheDocument();
  });

  it("点版本标签按该版本重新拉取报告", async () => {
    const user = userEvent.setup();
    mockApi(reportWith(GOOD_PAYLOAD));
    renderPage();
    await screen.findByText("版本 2");

    await user.click(screen.getByText("版本 2"));

    await waitFor(() =>
      expect(httpMock.get).toHaveBeenCalledWith("/research/sessions/s1/report", { params: { version: 2 } }),
    );
  });

  it("无报告时渲染空态", async () => {
    mockApi(null, []);
    renderPage();

    expect(await screen.findByText("暂无报告内容")).toBeInTheDocument();
  });

  it("路由缺 id 时不发请求", async () => {
    mockApi(null, []);
    renderPage("/research/report");

    await waitFor(() => expect(screen.getByText("研究报告")).toBeInTheDocument());
    expect(httpMock.get).not.toHaveBeenCalled();
  });

  // --- A1.1：坏 payload 不得崩渲染（迁移前 RED） ---------------------------

  it("payload 缺 sections 时收窄为安全默认值，不崩", async () => {
    mockApi(reportWith({ title: "", question: "" }));  // 无 sections
    renderPage();

    // 迁移前：(payload as unknown as ReportPayload).sections === undefined
    //          ⇒ ReportRenderer 内 sections.map 抛错 ⇒ render() 直接失败。
    // 迁移后：parseReportPayload ⇒ sections: []，标题回落。
    expect(await screen.findByText("研究报告")).toBeInTheDocument();
  });

  it("sections 不是数组时收窄为空，未知 block.type 被丢弃", async () => {
    mockApi(
      reportWith({
        title: "半坏报告", question: "q",
        sections: [
          { id: "s", kind: "summary", title: "执行摘要",
            blocks: [{ type: "wormhole", content: "??", sourceRefs: [] }] },
        ],
      }),
    );
    renderPage();

    expect(await screen.findAllByText("半坏报告")).toHaveLength(2);
    // 该 section 的 blocks 被全部丢弃 ⇒ 渲染 section 级空文案
    expect(screen.getByText("暂无报告内容")).toBeInTheDocument();
  });

  it("sections 本身不是数组时收窄为空数组", async () => {
    mockApi(reportWith({ title: "怪报告", question: "q", sections: "not-an-array" }));
    renderPage();

    expect(await screen.findAllByText("怪报告")).toHaveLength(2);
    expect(screen.queryByTestId("report-block")).toBeNull();
  });

  it("渲染报告的模式 Tag（payload.mode）", async () => {
    // mockApi 的签名以该文件既有写法为准；只需让报告 payload 的 mode 变为 attribution。
    mockApi(reportWith({ ...GOOD_PAYLOAD, mode: "attribution" }));
    renderPage();

    expect(await screen.findByText("归因")).toBeInTheDocument();
  });

  it("渲染模式 Tag 与对应 modeDesc 说明文字（A5：复用下拉项同一 key）", async () => {
    mockApi(reportWith({ ...GOOD_PAYLOAD, mode: "attribution" }));
    renderPage();

    expect(await screen.findByText("归因")).toBeInTheDocument();
    expect(
      screen.getByText("结论 → 假设验证表 → 数据 → 备选假设"),
    ).toBeInTheDocument();
  });

  it("payload.mode 未知：不渲染模式 Tag（不构造文案）", async () => {
    mockApi(reportWith({ ...GOOD_PAYLOAD, mode: "brand_new_mode" }));
    renderPage();

    // 标题恒有两处（页面头部 + ReportRenderer 的 <h2>），与本文件其他用例同口径。
    expect(await screen.findAllByText("本月收货量分析报告")).toHaveLength(2);
    // 既不显示原始 mode，也不把未命中的 i18n key 当文案渲染出来。
    expect(screen.queryByText("brand_new_mode")).not.toBeInTheDocument();
    expect(screen.queryByText("research.list.mode.brand_new_mode")).not.toBeInTheDocument();
  });
});
