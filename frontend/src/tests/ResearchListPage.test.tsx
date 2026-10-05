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
    question: "供应商 360° 全景", datasourceId: 1,
    createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-01T00:00:00Z",
  },
  {
    id: "s2", title: "", mode: "attribution", status: "succeeded",
    question: "为什么下降", createdAt: "2026-01-02T00:00:00Z", updatedAt: "2026-01-02T00:00:00Z",
  },
];

const DATASOURCES = [
  { id: 1, name: "THBI Oracle", type: "oracle", isDefault: true, isActive: true },
  { id: 2, name: "PG 报表库", type: "postgresql", isDefault: false, isActive: true },
];

// 按 URL 分发：数据源清单走 /datasources，其余（会话列表）走 SESSIONS。
function mockGet(overrides: { sessions?: unknown; datasources?: unknown } = {}) {
  httpMock.get.mockImplementation((url: string) => {
    if (url === "/datasources") {
      return Promise.resolve({ data: overrides.datasources ?? DATASOURCES });
    }
    return Promise.resolve({ data: overrides.sessions ?? SESSIONS });
  });
}

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
    mockGet();
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
    mockGet({ sessions: [] });
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
        datasourceId: 1,
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

  it("删除：二次确认后调 DELETE 并从列表移除", async () => {
    const user = userEvent.setup();
    httpMock.delete.mockResolvedValue({ data: null });
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    // 每行删除按钮的 aria-label 带行标识（title 优先，空则回落 question），
    // 故列表多行时仍能唯一定位到 s1 那一行。
    await user.click(screen.getByRole("button", { name: "删除研究会话：供应商 360°" }));
    // Popconfirm 的确定按钮：antd 会给两字中文按钮插空格 ⇒ 可访问名是「确 定」。
    await user.click(await screen.findByRole("button", { name: /确\s*定/ }));

    await waitFor(() =>
      expect(httpMock.delete).toHaveBeenCalledWith("/research/sessions/s1"),
    );
    await waitFor(() => expect(screen.queryByText("供应商 360°")).not.toBeInTheDocument());
    // s2 未删，仍在列表（其 title 为空 ⇒ Meta 的 title 与 description 都落到 question，两个文本节点）
    expect(screen.getAllByText("为什么下降")).toHaveLength(2);
  });

  it("删除失败：提示错误且列表保持原样（不做乐观移除）", async () => {
    const user = userEvent.setup();
    httpMock.delete.mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "删除研究会话：供应商 360°" }));
    await user.click(await screen.findByRole("button", { name: /确\s*定/ }));

    expect(await screen.findByText("删除失败")).toBeInTheDocument();
    expect(screen.getByText("供应商 360°")).toBeInTheDocument();
  });

  it("模式选项带副描述，且页面说明三模式只改报告章节结构", async () => {
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    // 选中项 label 是两行渲染：主标题 + 副描述（副描述在收起状态下也在 DOM 里）。
    expect(screen.getByText("执行摘要 → 数据 → 引用知识 → 方法学")).toBeInTheDocument();
    expect(
      screen.getByText("三者共用同一条研究流水线，仅改变报告的章节组织，不改变分析行为。"),
    ).toBeInTheDocument();
  });

  it("新建表单默认选中默认数据源；列表项显示所用数据源名", async () => {
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalledWith("/datasources", { params: { activeOnly: true } }));

    // 「THBI Oracle」出现 2 处：新建表单选中项 + s1（datasourceId=1）的 Tag。
    await waitFor(() => expect(screen.getAllByText("THBI Oracle")).toHaveLength(2));
    // s2 无 datasourceId ⇒ 不渲染源 Tag，故「PG 报表库」不出现。
    expect(screen.queryByText("PG 报表库")).not.toBeInTheDocument();
  });
});
