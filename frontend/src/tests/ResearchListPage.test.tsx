// ResearchListPage 页面级测试（feat-research-entry Task 15 / B3 覆盖率回填）。
//
// 本页此前 0% 覆盖，前端 80% 门槛因此「不可评估」。覆盖：挂载拉列表、空态、
// 发起新研究（含空/纯空白问题短路）、创建失败提示且不导航、多选 ≥2 才启用「对比」
// 并跳转 /research/compare?ids=…。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import ResearchListPage from "../pages/research/ResearchListPage";
import { useResearchStore } from "../stores/researchStore";

const httpMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock("../api/client", () => ({ httpClient: httpMock, apiClient: httpMock }));

const SESSIONS = [
  {
    id: "s1", title: "供应商 360°", mode: "research", status: "succeeded",
    question: "供应商 360° 全景", createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-01T00:00:00Z",
  },
  {
    id: "s2", title: "", mode: "attribution", status: "succeeded",
    question: "为什么下降", createdAt: "2026-01-02T00:00:00Z", updatedAt: "2026-01-02T00:00:00Z",
  },
];

// 探针：把当前路由暴露给断言（Routes 只渲染命中的那条，故放在 Routes 之外）。
function Probe() {
  const loc = useLocation();
  return <div data-testid="probe">{loc.pathname}{loc.search}</div>;
}

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <App>
        <MemoryRouter initialEntries={["/research"]}>
          <Routes>
            <Route path="/research" element={<ResearchListPage />} />
            <Route path="/research/compare" element={<div data-testid="compare-page" />} />
            <Route path="/research/:id" element={<div data-testid="session-page" />} />
          </Routes>
          <Probe />
        </MemoryRouter>
      </App>
    </ConfigProvider>,
  );
}

describe("ResearchListPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useResearchStore.getState().reset();
    httpMock.get.mockResolvedValue({ data: SESSIONS });
    httpMock.post.mockResolvedValue({ data: { ...SESSIONS[0], id: "s9" } });
  });

  it("挂载即拉取会话列表；title 为空时回落 question", async () => {
    renderPage();

    await waitFor(() => expect(httpMock.get).toHaveBeenCalledWith("/research/sessions"));
    expect(screen.getByText("供应商 360°")).toBeInTheDocument();
    // s2 的 title 为空 ⇒ meta.title 与 description 都落到 question（两个文本节点）
    expect(screen.getAllByText("为什么下降")).toHaveLength(2);
    // 「研究」出现两处：Select 的选中项（默认 mode=research）+ s1 的模式 Tag。
    expect(screen.getAllByText("研究")).toHaveLength(2);
    expect(screen.getByText("归因")).toBeInTheDocument();
  });

  it("无会话时渲染空态文案", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    renderPage();

    expect(await screen.findByText("暂无研究会话")).toBeInTheDocument();
  });

  it("输入问题后点「开始研究」：POST 会话并带 state 跳转到会话页", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    await user.type(screen.getByPlaceholderText("输入你的研究问题…"), "供应商 360° 全景");
    await user.click(screen.getByRole("button", { name: "开始研究" }));

    await waitFor(() =>
      expect(httpMock.post).toHaveBeenCalledWith("/research/sessions", {
        question: "供应商 360° 全景",
        mode: "research",
      }),
    );
    expect(await screen.findByTestId("session-page")).toBeInTheDocument();
    expect(screen.getByTestId("probe").textContent).toBe("/research/s9");
  });

  it("问题为空或纯空白时点「开始研究」不发请求", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "开始研究" }));
    expect(httpMock.post).not.toHaveBeenCalled();

    await user.type(screen.getByPlaceholderText("输入你的研究问题…"), "   ");
    await user.click(screen.getByRole("button", { name: "开始研究" }));
    expect(httpMock.post).not.toHaveBeenCalled();
    expect(screen.getByTestId("probe").textContent).toBe("/research");
  });

  it("创建失败：提示错误且不导航", async () => {
    const user = userEvent.setup();
    httpMock.post.mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    await user.type(screen.getByPlaceholderText("输入你的研究问题…"), "供应商 360° 全景");
    await user.click(screen.getByRole("button", { name: "开始研究" }));

    expect(await screen.findByText("出错了")).toBeInTheDocument();
    expect(screen.getByTestId("probe").textContent).toBe("/research");
  });

  it("选中 ≥2 个会话后「对比」可用并跳转 compare?ids=", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    // antd 对「两个汉字」的按钮文案自动插空格 ⇒ 可访问名是「对 比」，用正则匹配。
    const compareButton = screen.getByRole("button", { name: /对\s*比/ });
    expect(compareButton).toBeDisabled();

    await user.click(screen.getByLabelText("供应商 360°"));
    expect(compareButton).toBeDisabled(); // 只选 1 个仍禁用

    await user.click(screen.getByLabelText("为什么下降"));
    await waitFor(() => expect(compareButton).toBeEnabled());

    await user.click(compareButton);
    expect(await screen.findByTestId("compare-page")).toBeInTheDocument();
    expect(screen.getByTestId("probe").textContent).toBe("/research/compare?ids=s1,s2");
  });
});
