import { beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import AppLayout from "../components/common/AppLayout";
import ThemedRoot from "../components/common/ThemedRoot";
import { THEME_STORAGE_KEY, useThemeStore } from "../stores/themeStore";

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

    // AppLayout 仅渲染一个 <header>（antd Layout.Header），背景随 token 切换。
    // 断言"前后不同"而非硬编码 antd 内部 colorBgContainer 推导色值（#fff/#141414），
    // 避免 antd 调色变更时测试误报（5.8 审查 MEDIUM 修复）。
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
