import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AppLayout from "../components/common/AppLayout";

// Provide a real menu config so AppLayout (which no longer falls back to
// FALLBACK_NAV since feat-user-onboarding 2026-09-20) can render the items
// the test clicks (「本体管理」/「数据源」).
const menuConfigMock = vi.hoisted(() =>
  vi.fn().mockResolvedValue({
    version: "2026-09-20",
    sections: [
      {
        code: "section.aiAgent",
        labelKey: "menu.section.aiAgent",
        iconCode: "robot",
        sortOrder: 100,
        permissionCode: null,
        roles: [],
        path: null,
        children: [
          {
            code: "item.chat",
            labelKey: "menu.item.chat",
            iconCode: "message",
            sortOrder: 110,
            permissionCode: null,
            roles: [],
            path: "/chat",
          },
        ],
      },
      {
        code: "section.bizConfig",
        labelKey: "menu.section.bizConfig",
        iconCode: "setting",
        sortOrder: 300,
        permissionCode: null,
        roles: [],
        path: null,
        children: [
          {
            code: "item.ontology",
            labelKey: "menu.item.ontology",
            iconCode: "partition",
            sortOrder: 310,
            permissionCode: null,
            roles: [],
            path: "/ontology",
          },
          {
            code: "item.datasource",
            labelKey: "menu.item.datasource",
            iconCode: "database",
            sortOrder: 320,
            permissionCode: null,
            roles: [],
            path: "/datasource",
          },
        ],
      },
    ],
  }),
);
vi.mock("../api/menuConfig", () => ({
  fetchMenuConfig: menuConfigMock,
}));
import ChatPage from "../pages/ChatPage";
import OntologyPage from "../pages/OntologyPage";
import DatasourcePage from "../pages/DatasourcePage";

// mock datasource API，避免网络调用
const datasourceApi = vi.hoisted(() => ({
  listDataSources: vi.fn().mockResolvedValue([]),
}));
vi.mock("../api/datasource", () => datasourceApi);

// mock ontology API，避免网络调用
const api = vi.hoisted(() => ({
  listClasses: vi.fn().mockResolvedValue([]),
  listMetrics: vi.fn().mockResolvedValue([]),
  listPropertiesByClass: vi.fn().mockResolvedValue([]),
  getClass: vi.fn(),
  createClass: vi.fn(),
  updateClass: vi.fn(),
  deleteClass: vi.fn(),
  getProperty: vi.fn(),
  createProperty: vi.fn(),
  updateProperty: vi.fn(),
  deleteProperty: vi.fn(),
  getMetric: vi.fn(),
  createMetric: vi.fn(),
  updateMetric: vi.fn(),
  deleteMetric: vi.fn(),
}));
vi.mock("../api/ontology", () => api);

function renderWithRouter(initial = "/") {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/" element={<AppLayout />}>
            <Route index element={<div data-testid="home">home</div>} />
            <Route path="chat" element={<ChatPage />} />
            <Route path="ontology" element={<OntologyPage />} />
            <Route path="datasource" element={<DatasourcePage />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("AppLayout 导航", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 清理 openKeys — feat-user-onboarding-ext (2026-09-20)：AppLayout 不再
    // 从 localStorage 读初始 openKeys（每次 mount reset 到
    // DEFAULT_OPENED_SECTIONS = [section.aiAgent]）。本测试在 render 后
    // 主动点开「业务配置」section 来确保子项可点击。
    localStorage.removeItem("menu.openKeys");
  });

  it("默认显示侧边栏与首页占位", () => {
    renderWithRouter();
    expect(screen.getByText("AI服务平台")).toBeInTheDocument();
    expect(screen.getByTestId("home")).toBeInTheDocument();
  });

  it("点击菜单项切换到本体管理页面", async () => {
    const user = userEvent.setup();
    renderWithRouter();
    // 默认只有 AI Agent 展开；先点开「业务配置」section 让子项「本体管理」可见
    await user.click(await screen.findByText("业务配置"));
    await waitFor(() => expect(screen.getByText("本体管理")).toBeInTheDocument());
    await user.click(screen.getByText("本体管理"));
    // 用 Tabs 标签（sidebar + header 都没有）确认页面已切换
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /类/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /属性/ })).toBeInTheDocument();
    });
    // 再点开「数据源」（之前点过业务配置已经展开，「数据源」仍可见）
    await user.click(screen.getByText("数据源"));
    await waitFor(() => {
      expect(screen.getByText("数据源管理")).toBeInTheDocument();
    });
  });
});

describe("占位页面渲染", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("ChatPage 显示问答界面", async () => {
    renderWithRouter("/chat");
    // 标题与输入区（菜单栏也含 "AIChatService"，故用输入框占位符断言）
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/输入自然语言问题/)).toBeInTheDocument();
      expect(screen.getByText("输入问题开始对话")).toBeInTheDocument();
    });
  });

  it("OntologyPage 显示本体管理标签页", async () => {
    renderWithRouter("/ontology");
    // 确认 Tabs 渲染（sidebar 没有 tab role）
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /类/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /属性/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /指标/ })).toBeInTheDocument();
    });
  });

  it("DatasourcePage 显示数据源管理页", async () => {
    renderWithRouter("/datasource");
    expect(screen.getByText("数据源管理")).toBeInTheDocument();
    expect(screen.getByText("新增数据源")).toBeInTheDocument();
  });
});
