import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "../App";
import { useAuthStore } from "../stores/authStore";

const api = vi.hoisted(() => ({
  // ChatPanel 在 /chat 路由加载时调用 listModels(true) 拉可聊模型列表；
  // 默认路由从 /models 改成 /chat 后这个 mock 必须返回 []，否则 ChatPanel
  // useEffect 里 `.then((data) => ...)` 抛 TypeError。
  listModels: vi.fn().mockResolvedValue([]),
  createModel: vi.fn(),
  updateModel: vi.fn(),
  deactivateModel: vi.fn(),
}));
vi.mock("../api/modelConfig", () => api);
// 静默 chat 相关 fetch（ChatPage 内部用 chatStore + API，本测试只关心路由重定向）
vi.mock("../api/chatHistory", () => ({ exportSessionPdf: vi.fn() }));
vi.mock("../api/chat", () => ({
  sendChatMessage: vi.fn(),
  sendChatMessageStream: vi.fn(),
  listChatModels: vi.fn().mockResolvedValue([]),
  describeTable: vi.fn(),
  getChartConfig: vi.fn(),
  previewChart: vi.fn(),
  getSuggestions: vi.fn().mockResolvedValue([]),
}));
vi.mock("../api/datasource", () => ({
  listDataSources: vi.fn().mockResolvedValue([]),
}));
// AppLayout mount 时拉菜单；测试环境不挂真实后端，mock 返回空 sections 让 Sider 正常渲染
vi.mock("../api/menuConfig", () => ({
  fetchMenuConfig: vi.fn().mockResolvedValue({ sections: [], items: [] }),
}));

// 页面级 mock：只关心路由命中哪个页面，不关心页面内部的数据加载。
// 组件返回字符串（ReactNode），避免在 vi.mock 工厂里写 JSX。
vi.mock("../pages/AgentOrchestrationPage", () => ({
  default: () => "AgentOrchestrationPage",
}));
vi.mock("../pages/AgentTaskPage", () => ({
  default: () => "AgentTaskPage",
}));
vi.mock("../pages/AgentRuntimePage", () => ({
  default: () => "AgentRuntimePage",
}));
vi.mock("../pages/AgentRegistryPage", () => ({
  default: () => "AgentRegistryPage",
}));

// 哨兵组件：渲染到 <App /> 之外（与 AppLayout 并列），把当前 pathname 暴露给测试。
// 不能直接断言 AppLayout Header（受 selectedItem 计算影响，复杂），
// 也不能直接渲染 ChatPage（依赖 chatStore 数据，mock 链长）。
// 走 MemoryRouter + 一个独立 Routes 容器，在 /__test 路径下挂这个哨兵，
// 即可观察到 App.tsx 的 <Navigate> 重定向到了哪个路径。
function LocationProbe(): JSX.Element {
  const { pathname } = useLocation();
  return <div data-testid="probe-path">{pathname}</div>;
}

describe("App 路由", () => {
  beforeEach(() => {
    // feat-user-auth: RequireAuth 守卫需要登录态；测试前注入 stub token + user
    useAuthStore.setState({
      token: "test-token",
      user: {
        id: 1,
        username: "admin",
        displayName: "系统管理员",
        email: null,
        enabled: true,
        mustChangePassword: false,
        roles: ["admin"],
        organizations: ["IT"],
        tenantId: "default",
        lastLoginAt: null,
      },
      mustChangePassword: false,
      rememberMe: true,
    });
  });

  it("默认重定向到 /chat (AIChatService)", async () => {
    // feat-user-onboarding (2026-09-20)：登录后默认页从 /models 改为 /chat
    // （admin 经常切模型配置；普通用户进入即用 AIChatService）。
    render(
      <ConfigProvider locale={zhCN}>
        <MemoryRouter initialEntries={["/"]}>
          <App />
          <LocationProbe />
        </MemoryRouter>
      </ConfigProvider>
    );

    // AppLayout 标题存在（setup.ts 修复后 useTranslation 走真实 i18next，返回中文）
    expect(screen.getByText("AI服务平台")).toBeInTheDocument();
    // 默认导航跳到了 /chat（不再是 /models）
    const probe = await screen.findByTestId("probe-path");
    expect(probe.textContent).toBe("/chat");
  });

  it("未知路径兜底重定向到 /（已登录最终落 /chat）", async () => {
    // fix/login-401-white-screen：没有 * 兜底路由时未知路径（如旧的 /auth/login）
    // 一律白屏无提示。
    render(
      <ConfigProvider locale={zhCN}>
        <MemoryRouter initialEntries={["/no-such-page"]}>
          <App />
          <LocationProbe />
        </MemoryRouter>
      </ConfigProvider>
    );
    const probe = await screen.findByTestId("probe-path");
    expect(probe.textContent).toBe("/chat");
  });

  it("未知路径兜底（未登录经 RequireAuth 落 /login）", async () => {
    useAuthStore.setState({ token: null, user: null, mustChangePassword: false });
    render(
      <ConfigProvider locale={zhCN}>
        <MemoryRouter initialEntries={["/no-such-page"]}>
          <App />
          <LocationProbe />
        </MemoryRouter>
      </ConfigProvider>
    );
    const probe = await screen.findByTestId("probe-path");
    expect(probe.textContent).toBe("/login");
  });

  // 通用 Agent 编排：两个新页面必须能被直接深链访问，且不夺走旧 Agent 路由。
  const ROUTE_CASES: { path: string; marker: string }[] = [
    { path: "/agent-orchestrations", marker: "AgentOrchestrationPage" },
    { path: "/agent-tasks", marker: "AgentTaskPage" },
    { path: "/agents/run", marker: "AgentRuntimePage" },
    { path: "/agents", marker: "AgentRegistryPage" },
  ];

  it.each(ROUTE_CASES)("直接访问 $path 渲染 $marker", async ({ path, marker }) => {
    render(
      <ConfigProvider locale={zhCN}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </ConfigProvider>
    );
    expect(await screen.findByText(marker)).toBeInTheDocument();
  });
});
