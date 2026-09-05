/** ThemedRoot 单元测试（Phase A RED）。
 *
 * 覆盖：
 * - 暗色状态：使用 darkAlgorithm + DARK_TOKEN + 注入 --color-primary 等 CSS 变量
 * - 亮色状态：使用 defaultAlgorithm + LIGHT_TOKEN + 注入亮色 CSS 变量
 * - 切换 toggleTheme 后，主题与 CSS 变量同步更新
 * - 卸载时 CSS 变量被清理（避免下次挂载看到残留）
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { act, render } from "@testing-library/react";
import ThemedRoot from "../components/common/ThemedRoot";
import { useThemeStore } from "../stores/themeStore";

describe("ThemedRoot 主题切换", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.style.cssText = "";
    useThemeStore.setState({ isDark: false });
  });

  afterEach(() => {
    document.documentElement.style.cssText = "";
  });

  it("初始默认暗色模式：写入 DARK_TOKEN 对应的 CSS 变量", () => {
    // 注意：默认改为 isDark=true（暗色为主）。这里测试默认态。
    act(() => {
      useThemeStore.setState({ isDark: true });
    });
    render(
      <ThemedRoot>
        <div data-testid="child">content</div>
      </ThemedRoot>,
    );

    const inline = document.documentElement.style.cssText;
    expect(inline).toContain("--color-primary: #00D9C0");
    expect(inline).toContain("--color-bg-base: #0f1e2e");
    expect(inline).toContain("--color-bg-container: #152838");
    expect(inline).toContain("--border-radius: 2px");
  });

  it("亮色模式：写入 LIGHT_TOKEN 对应的 CSS 变量", () => {
    act(() => {
      useThemeStore.setState({ isDark: false });
    });
    render(
      <ThemedRoot>
        <div>content</div>
      </ThemedRoot>,
    );

    const inline = document.documentElement.style.cssText;
    expect(inline).toContain("--color-primary: #00B8A9");
    expect(inline).toContain("--color-bg-base: #f5f7fa");
    expect(inline).toContain("--color-bg-container: #ffffff");
  });

  it("toggleTheme 后 CSS 变量同步从 DARK 切换为 LIGHT", () => {
    act(() => {
      useThemeStore.setState({ isDark: true });
    });
    const { rerender } = render(
      <ThemedRoot>
        <div>content</div>
      </ThemedRoot>,
    );

    expect(document.documentElement.style.cssText).toContain("#00D9C0");

    // 切到亮色（toggleTheme 是 store action，会触发订阅者 rerender；用 act 包裹）
    act(() => {
      useThemeStore.getState().toggleTheme();
    });
    rerender(
      <ThemedRoot>
        <div>content</div>
      </ThemedRoot>,
    );

    const inline = document.documentElement.style.cssText;
    // 主色变 light
    expect(inline).toContain("#00B8A9");
    // 暗色背景不残留
    expect(inline).not.toContain("#0f1e2e");
  });

  it("卸载 ThemedRoot 时 CSS 变量被清理", () => {
    act(() => {
      useThemeStore.setState({ isDark: true });
    });
    const { unmount } = render(
      <ThemedRoot>
        <div>content</div>
      </ThemedRoot>,
    );

    expect(document.documentElement.style.cssText).toContain("--color-primary");

    unmount();

    expect(document.documentElement.style.cssText).not.toContain("--color-primary");
    expect(document.documentElement.style.cssText).not.toContain("--color-bg-base");
  });

  it("渲染包裹子元素而不修改其 props", () => {
    act(() => {
      useThemeStore.setState({ isDark: true });
    });
    render(
      <ThemedRoot>
        <div data-testid="child">content</div>
      </ThemedRoot>,
    );
    // ConfigProvider 必须透传子节点
    expect(document.querySelector("[data-testid='child']")).not.toBeNull();
  });
});