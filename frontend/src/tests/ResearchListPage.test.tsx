// ResearchListPage 页面级测试（feat-research-entry Task 15 / B3 覆盖率回填）。
//
// 本页此前 0% 覆盖，前端 80% 门槛因此「不可评估」。覆盖：发起新研究（含空/纯
// 空白问题短路）、创建失败提示且不导航、模式选项带副描述、数据源默认选中、
// 模型「自动」默认值与请求体 modelId 键的行为。
//
// 历史会话不在本页：迁到 /research/history 独立路由（ResearchHistoryPage），
// 多选/删除/对比的覆盖在 ResearchHistoryPage.test.tsx。本页只测「创建 + 跳转 +
// 新增入口」三件事。
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

const MODELS = [
  { id: 1, modelName: "deepseek-chat", isActive: true },
  { id: 2, modelName: "minimax-m1", isActive: true },
];

// 按 URL 分发：数据源清单走 /datasources，模型清单走 /models，其余（会话列表）走 SESSIONS。
function mockGet(
  overrides: { sessions?: unknown; datasources?: unknown; models?: unknown } = {},
) {
  httpMock.get.mockImplementation((url: string) => {
    if (url === "/datasources") {
      return Promise.resolve({ data: overrides.datasources ?? DATASOURCES });
    }
    if (url === "/models") {
      return Promise.resolve({ data: overrides.models ?? MODELS });
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
            <Route path="/research/history" element={<div data-testid="history-page" />} />
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

  it("进列表页不主动拉 sessions：历史会话入口在 /research/history", async () => {
    renderPage();
    // 等到 datasources/models 这两个无副作用的 GET 至少各打了一次，证明 mount
    // 完成；断言 sessions 列表接口**未**被打（历史会话拆走，本页不做 list）。
    await waitFor(() => expect(httpMock.get).toHaveBeenCalledWith("/datasources", { params: { activeOnly: true } }));
    expect(httpMock.get).not.toHaveBeenCalledWith("/research/sessions");
  });

  it("输入问题后点「开始研究」：POST 会话并带 state 跳转到会话页", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    await user.type(screen.getByPlaceholderText("输入你的研究问题…"), "供应商 360° 全景");
    await user.click(screen.getByRole("button", { name: "开始研究" }));

    // 精确对象匹配：不选模型时请求体里不应出现 modelId 键（「自动」与今天逐字一致）。
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

  it("模式选项带副描述，且页面说明三模式只改报告章节结构", async () => {
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    // 选中项 label 是两行渲染：主标题 + 副描述（副描述在收起状态下也在 DOM 里）。
    expect(screen.getByText("执行摘要 → 数据 → 引用知识 → 方法学")).toBeInTheDocument();
    expect(
      screen.getByText("三者共用同一条研究流水线，仅改变报告的章节组织，不改变分析行为。"),
    ).toBeInTheDocument();
  });

  it("新建表单默认选中默认数据源", async () => {
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalledWith("/datasources", { params: { activeOnly: true } }));

    // 新建表单的 Select 选中项渲染 THBI Oracle；s1/s2 都不在本页（迁到 history）。
    await waitFor(() => expect(screen.getByText("THBI Oracle")).toBeInTheDocument());
  });

  it("模型下拉默认「自动」；不选时请求体不带 modelId 键", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() =>
      expect(httpMock.get).toHaveBeenCalledWith("/models", { params: { activeOnly: true } }),
    );

    expect(await screen.findByText("自动（智能路由）")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("输入你的研究问题…"), "供应商 360° 全景");
    await user.click(screen.getByRole("button", { name: "开始研究" }));

    await waitFor(() => {
      const [, body] = httpMock.post.mock.calls[0];
      expect(body).not.toHaveProperty("modelId");
    });
  });

  it("选定模型后请求体带 modelId", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    await user.click(screen.getByText("自动（智能路由）"));
    await user.click(await screen.findByText("minimax-m1"));

    await user.type(screen.getByPlaceholderText("输入你的研究问题…"), "供应商 360° 全景");
    await user.click(screen.getByRole("button", { name: "开始研究" }));

    await waitFor(() =>
      expect(httpMock.post).toHaveBeenCalledWith("/research/sessions", {
        question: "供应商 360° 全景",
        mode: "research",
        datasourceId: 1,
        modelId: 2,
      }),
    );
  });

  it("点「历史会话」按钮跳到 /research/history", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    // antd 对四个汉字按钮自动插空格 ⇒ 可访问名带空格，用正则兼容。
    await user.click(screen.getByRole("button", { name: /历\s*史\s*会\s*话/ }));
    expect(await screen.findByTestId("history-page")).toBeInTheDocument();
    expect(screen.getByTestId("probe").textContent).toBe("/research/history");
  });
});
