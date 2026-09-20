import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import AppLayout from "../components/common/AppLayout";
import ThemedRoot from "../components/common/ThemedRoot";
import { THEME_STORAGE_KEY, useThemeStore } from "../stores/themeStore";
import { fetchMenuConfig } from "../api/menuConfig";

// Provide a default resolved value so existing tests that don't override work.
// Use mockImplementation (not mockResolvedValue) — it persists through vi.clearAllMocks()
// in the existing describe block's beforeEach, ensuring both test suites get isolated mocks.
vi.mock("../api/menuConfig", () => ({
  fetchMenuConfig: vi.fn().mockImplementation(() =>
    Promise.resolve({ version: "0", sections: [] }),
  ),
}));

// AppLayout 暗色模式切换（5.8）：Header 内 Switch 切换主题 → ConfigProvider algorithm
// → antd token 变化 → Header/Content 背景色切换 + localStorage 持久化
describe("AppLayout 暗色模式切换", () => {
  beforeEach(() => {
    localStorage.clear();
    useThemeStore.setState({ isDark: false });
  });

  function renderLayout() {
    return render(
      <ThemedRoot>
        <MemoryRouter initialEntries={["/models"]}>
          <Routes>
            <Route element={<AppLayout />}>
              <Route path="/models" element={<div>模型页</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ThemedRoot>
    );
  }

  it("渲染主题切换开关，初始为亮色", () => {
    renderLayout();

    const toggle = screen.getByRole("switch");
    expect(toggle).toBeInTheDocument();
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("点击切换后 Header 背景随主题变化并持久化到 localStorage", async () => {
    const user = userEvent.setup();
    renderLayout();

    const header = document.querySelector("header")!;
    expect(header).toBeInTheDocument();
    const initialBg = header.style.background;
    expect(initialBg).toBeTruthy();

    await user.click(screen.getByRole("switch"));

    expect(header.style.background).not.toBe(initialBg);
    expect(useThemeStore.getState().isDark).toBe(true);
    const stored = JSON.parse(localStorage.getItem(THEME_STORAGE_KEY) ?? "{}");
    expect(stored.state?.isDark).toBe(true);
  });
});

describe("AppLayout menu fetch + fallback", () => {
  const twoSectionConfig = {
    version: "2026-09-01",
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
          {
            code: "item.models",
            labelKey: "menu.item.models",
            iconCode: "robot",
            sortOrder: 120,
            permissionCode: null,
            roles: [],
            path: "/models",
          },
        ],
      },
      {
        code: "section.analytics",
        labelKey: "menu.section.analytics",
        iconCode: "alert",
        sortOrder: 200,
        permissionCode: null,
        roles: [],
        path: null,
        children: [
          {
            code: "item.dataQuality",
            labelKey: "menu.item.dataQuality",
            iconCode: "alert",
            sortOrder: 210,
            permissionCode: null,
            roles: [],
            path: "/data-quality",
          },
        ],
      },
    ],
  };

  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  function renderLayout(initialPath = "/chat") {
    return render(
      <ThemedRoot>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route element={<AppLayout />}>
              <Route path="/chat" element={<div>聊天页</div>} />
              <Route path="/models" element={<div>模型页</div>} />
              <Route path="/data-quality" element={<div>质量页</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ThemedRoot>
    );
  }

  it("renders submenus from fetched menu config", async () => {
    vi.mocked(fetchMenuConfig).mockResolvedValue(twoSectionConfig);
    renderLayout("/chat");

    // Section labels become submenu titles (real i18n keys: menu.section.aiAgent → "AI Agent")
    expect(await screen.findByText("AI Agent")).toBeInTheDocument();
    expect(screen.getByText("智能分析")).toBeInTheDocument();
  });

  it("shows error alert when fetch rejects (no flat fallback)", async () => {
    // feat-user-onboarding (2026-09-20): /menu-config 失败必须显式提示，
    // 永远不再回退 FALLBACK_NAV（避免出现与权限不符的扁平菜单误导用户）。
    vi.mocked(fetchMenuConfig).mockRejectedValue(new Error("network error"));
    renderLayout("/chat");

    // antd Alert 渲染 role="alert"，其文本被拆到 icon + message + description
    // 多 text node → 不能用 getByText；改用 role 匹配。test i18n 默认 zh-CN。
    const alert = await screen.findByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(alert.textContent).toContain("菜单加载失败");
    // 扁平菜单的兜底项绝对不能再渲染
    expect(screen.queryByText("模型配置")).not.toBeInTheDocument();
    expect(screen.queryByText("Embedding 服务")).not.toBeInTheDocument();
  });

  it("shows empty-state when DB returns no sections", async () => {
    // 合法场景：admin 没给某 user 任何 menu grant → 后端返回 sections=[]。
    // 此时显示「暂无授权菜单」+ 不再有扁平兜底；用户应去 /admin/users 或
    // /admin/menus 让 admin 给自己补授权。
    vi.mocked(fetchMenuConfig).mockResolvedValue({ version: "0", sections: [] });
    renderLayout("/chat");

    expect(await screen.findByText("暂无授权菜单")).toBeInTheDocument();
    expect(screen.queryByText("模型配置")).not.toBeInTheDocument();
  });

  it("默认仅 AI Agent 展开：用户手动展开其它 section 会写入 localStorage", async () => {
    // feat-user-onboarding-ext (2026-09-20 调整)：默认只展开 AI Agent
    // （默认页 /chat = AIChatService）。其他 section 起始折叠；用户手动
    // 点击展开其它 section 后，本次 session 内会写入 localStorage。
    vi.mocked(fetchMenuConfig).mockResolvedValue(twoSectionConfig);
    const user = userEvent.setup();
    renderLayout("/chat");

    // 默认 AI Agent 已展开；点击「智能分析」section 标题展开它
    const analyticsHeader = await screen.findByText("智能分析");
    await user.click(analyticsHeader);

    const stored = localStorage.getItem("menu.openKeys");
    expect(stored).not.toBeNull();
    const parsed = JSON.parse(stored!);
    expect(parsed).toContain("section.aiAgent");
    expect(parsed).toContain("section.analytics");
  });

  it("selected key matches current path", async () => {
    vi.mocked(fetchMenuConfig).mockResolvedValue(twoSectionConfig);
    renderLayout("/data-quality");

    // The data-quality child item should be reachable and rendered
    expect(await screen.findByText("数据质量")).toBeInTheDocument();
  });

  it("系统信息配置（section.systemConfig）始终折叠：localStorage 残留也过滤", async () => {
    // feat-user-onboarding-ext (2026-09-20)：系统信息配置 menu 默认折叠。
    // 即使用户之前点开过（localStorage 残留），下次登录读到 state 时也过滤掉。
    // 加 systemConfig 到 fixture，并预设 localStorage 残留 → 验证渲染后
    // section.systemConfig 不在 ant-menu-submenu-open 列表里。
    const withSystemConfig = {
      version: "2026-09-20",
      sections: [
        ...twoSectionConfig.sections,
        {
          code: "section.systemConfig",
          labelKey: "menu.section.systemConfig",
          iconCode: "api",
          sortOrder: 500,
          permissionCode: null,
          roles: [],
          path: null,
          children: [
            {
              code: "item.models",
              labelKey: "menu.item.models",
              iconCode: "robot",
              sortOrder: 510,
              permissionCode: null,
              roles: [],
              path: "/models",
            },
          ],
        },
      ],
    };
    localStorage.setItem(
      "menu.openKeys",
      JSON.stringify(["section.systemConfig", "section.aiAgent"]),
    );
    vi.mocked(fetchMenuConfig).mockResolvedValue(withSystemConfig);
    renderLayout("/chat");

    // 等到「系统信息配置」标题出现，再检查它的展开状态。
    expect(await screen.findByText("系统信息配置")).toBeInTheDocument();
    // 找到包含「系统信息配置」文字的 ant-menu-submenu 节点，断言无 open className。
    const sysCfgSubmenus = Array.from(
      document.querySelectorAll(".ant-menu-submenu"),
    ).filter((el) => (el.textContent ?? "").includes("系统信息配置"));
    expect(sysCfgSubmenus.length).toBeGreaterThan(0);
    sysCfgSubmenus.forEach((el) => {
      expect(el.classList.contains("ant-menu-submenu-open")).toBe(false);
    });
    // 同时确认「AI Agent」section 因 localStorage 残留确实展开（不受误伤）
    const aiAgentOpened = Array.from(
      document.querySelectorAll(".ant-menu-submenu-open"),
    ).some((el) => (el.textContent ?? "").includes("AI Agent"));
    expect(aiAgentOpened).toBe(true);
  });
});
