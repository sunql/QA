// ResearchHistoryPage 页面级测试（feat-research-entry-ux-fixes 后续：历史会话
// 拆成独立路由 /research/history 后落地）。
//
// 覆盖：mount 拉列表（store 预填）、空态、点行 navigate、点行内打开按钮 navigate、
// 点行内报告按钮 navigate 到 /report、多选 ≥2 启用「对比」并跳转 compare?ids=…、
// 删除（含二次确认 + 失败回滚）、「返回」按钮回 /research。
//
// 重要：本页是「历史会话」的承载；ResearchListPage 不再渲染列表、不再做
// 多选 / 删除 / 对比。关注点分离的测试契约。
//
// 测试隔离坑：loadSessions 是个 await Promise.resolve(...) 链。如果让 useEffect
// 触发它，Promise 会卡在 vitest 微任务队列里，测试跑完也不 drain，整个 file
// 卡死。所以**默认 beforeEach 把 store 预填成 SESSIONS**，useEffect 看 sessions
// 非空就跳过 loadSessions。只有「empty」case 显式置空 + 自管 mock。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import ResearchHistoryPage from "../pages/research/ResearchHistoryPage";
import { useResearchStore } from "../stores/researchStore";
import type { ResearchSession } from "../types/research";

const httpMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock("../api/client", () => ({ httpClient: httpMock, apiClient: httpMock }));

const SESSIONS: ResearchSession[] = [
  {
    id: "s1", title: "供应商 360°", mode: "research", status: "done",
    question: "供应商 360° 全景", datasourceId: 1,
    createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-01T00:00:00Z",
  },
  {
    id: "s2", title: "", mode: "attribution", status: "done",
    question: "为什么下降", createdAt: "2026-01-02T00:00:00Z", updatedAt: "2026-01-02T00:00:00Z",
  },
];

const DATASOURCES = [
  { id: 1, name: "THBI Oracle", type: "oracle", isDefault: true, isActive: true },
  { id: 2, name: "PG 报表库", type: "postgresql", isDefault: false, isActive: true },
];

// 按 URL 分发：数据源走 /datasources，会话列表走 SESSIONS。
// 注意：默认 beforeEach 已把 store 预填好，**不会**真调到这里；只有显式
// 「mount 时拉列表」那个 case 走这里，并且它只校验 httpMock 是否被调。
function mockGet(
  overrides: { sessions?: unknown; datasources?: unknown } = {},
) {
  httpMock.get.mockImplementation((url: string) => {
    if (url === "/datasources") {
      return Promise.resolve({ data: overrides.datasources ?? DATASOURCES });
    }
    return Promise.resolve({ data: overrides.sessions ?? SESSIONS });
  });
}

function Probe() {
  const loc = useLocation();
  return <div data-testid="probe">{loc.pathname}{loc.search}</div>;
}

function renderPage(initial = "/research/history") {
  return render(
    <ConfigProvider locale={zhCN}>
      <App>
        <MemoryRouter initialEntries={[initial]}>
          <Routes>
            <Route path="/research" element={<div data-testid="list-page" />} />
            <Route path="/research/history" element={<ResearchHistoryPage />} />
            <Route path="/research/compare" element={<div data-testid="compare-page" />} />
            <Route path="/research/:id" element={<div data-testid="session-page" />} />
            <Route path="/research/:id/report" element={<div data-testid="report-page" />} />
          </Routes>
          <Probe />
        </MemoryRouter>
      </App>
    </ConfigProvider>,
  );
}

describe("ResearchHistoryPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useResearchStore.getState().reset();
    // 预填 sessions：**关键** —— 否则 mount 时 useEffect 触发 loadSessions，
    // await 的 Promise.resolve 卡在 vitest 微任务队列里，整个 file 测试不结束。
    useResearchStore.setState({ sessions: SESSIONS, sessionsLoading: false });
    mockGet();
  });

  it("mount 时拉列表（sess=0 时触发）；title 空回落 question", () => {
    // 单测 loadSessions 真的发请求：先清空让 useEffect 触发它。
    useResearchStore.setState({ sessions: [], sessionsLoading: false });
    mockGet();
    renderPage();
    // 不 await Promise —— 我们只校验 loadSessions 是否真的打了 /research/sessions；
    // 真正的「列表渲染 + 标题回落」在下一个 case 覆盖（那边 pre-populate）。
    expect(httpMock.get).toHaveBeenCalledWith("/research/sessions");
  });

  it("列表渲染：title 为空时回落 question；模式 Tag 显示中文化名", () => {
    renderPage();

    // s1.title 非空 ⇒ Meta title=「供应商 360°」、description=「供应商 360° 全景」
    expect(screen.getByText("供应商 360°")).toBeInTheDocument();
    expect(screen.getByText("供应商 360° 全景")).toBeInTheDocument();
    // s2.title 为空 ⇒ Meta title/description 都落 question 「为什么下降」两次
    expect(screen.getAllByText("为什么下降")).toHaveLength(2);
    // 模式 Tag：s1=研究、s2=归因
    expect(screen.getByText("研究")).toBeInTheDocument();
    expect(screen.getByText("归因")).toBeInTheDocument();
  });

  it("空态：sess=[] 时显示「暂无研究会话」", () => {
    // useEffect 触发 loadSessions 会把 sessionsLoading 翻成 true，把 List 切到
    // loading 态遮住 Empty。这里**预填** sessions=[] + sessionsLoading=false + 把
    // loadSessions 替成 no-op，页面稳定停在「空态 + 非 loading」组合。
    useResearchStore.setState({
      sessions: [],
      sessionsLoading: false,
      loadSessions: () => Promise.resolve(),
    });
    renderPage();
    expect(screen.getByText("暂无研究会话")).toBeInTheDocument();
  });

  it("点行体 navigate 到 /research/:id", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByText("供应商 360°"));
    expect(await screen.findByTestId("session-page")).toBeInTheDocument();
    expect(screen.getByTestId("probe").textContent).toBe("/research/s1");
  });

  it("点行内「打开」按钮 navigate 到 /research/:id", async () => {
    const user = userEvent.setup();
    renderPage();

    // 多个「打开」按钮：精确找 s1 行（用 aria-label 收窄到行内「打开」按钮）。
    const s1Row = screen.getByText("供应商 360°").closest(".ant-list-item") as HTMLElement;
    expect(s1Row).not.toBeNull();
    await user.click(within(s1Row).getByRole("button", { name: "打开" }));
    expect(await screen.findByTestId("session-page")).toBeInTheDocument();
    expect(screen.getByTestId("probe").textContent).toBe("/research/s1");
  });

  it("点行内「报告」按钮 navigate 到 /research/:id/report", async () => {
    const user = userEvent.setup();
    renderPage();

    const s1Row = screen.getByText("供应商 360°").closest(".ant-list-item") as HTMLElement;
    expect(s1Row).not.toBeNull();
    await user.click(within(s1Row).getByRole("button", { name: "报告" }));
    expect(await screen.findByTestId("report-page")).toBeInTheDocument();
    expect(screen.getByTestId("probe").textContent).toBe("/research/s1/report");
  });

  it("选中 ≥2 个会话后「对比」可用并跳转 compare?ids=", async () => {
    const user = userEvent.setup();
    renderPage();

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

    await user.click(screen.getByRole("button", { name: "删除研究会话：供应商 360°" }));
    await user.click(await screen.findByRole("button", { name: /确\s*定/ }));

    expect(await screen.findByText("删除失败")).toBeInTheDocument();
    expect(screen.getByText("供应商 360°")).toBeInTheDocument();
  });

  it("「返回」按钮跳回 /research", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: /返\s*回/ }));
    expect(await screen.findByTestId("list-page")).toBeInTheDocument();
    expect(screen.getByTestId("probe").textContent).toBe("/research");
  });
});
