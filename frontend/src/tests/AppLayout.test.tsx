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

  it("falls back to static nav when fetch rejects", async () => {
    vi.mocked(fetchMenuConfig).mockRejectedValue(new Error("network error"));
    renderLayout("/chat");

    // Fallback: flat list items (t keys from FALLBACK_NAV)
    // FALLBACK_NAV[0] = { key: "/models", labelKey: "appLayout.menu.models" } → "模型配置"
    expect(await screen.findByText("模型配置")).toBeInTheDocument();
    expect(screen.getByText("Embedding 服务")).toBeInTheDocument();
  });

  it("persists openKeys to localStorage on submenu expand", async () => {
    vi.mocked(fetchMenuConfig).mockResolvedValue(twoSectionConfig);
    const user = userEvent.setup();
    renderLayout("/chat");

    // Open the "AI Agent" submenu by clicking its title
    const aiAgentItem = await screen.findByText("AI Agent");
    await user.click(aiAgentItem);

    const stored = localStorage.getItem("menu.openKeys");
    expect(stored).not.toBeNull();
    const parsed = JSON.parse(stored!);
    expect(parsed).toContain("section.aiAgent");
  });

  it("selected key matches current path", async () => {
    vi.mocked(fetchMenuConfig).mockResolvedValue(twoSectionConfig);
    renderLayout("/data-quality");

    // The data-quality child item should be reachable and rendered
    expect(await screen.findByText("数据质量")).toBeInTheDocument();
  });
});
