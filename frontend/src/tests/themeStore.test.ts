import { beforeEach, describe, expect, it } from "vitest";
import { THEME_STORAGE_KEY, useThemeStore } from "../stores/themeStore";

// 暗色模式主题 store（5.8）：isDark + toggleTheme + localStorage 持久化
describe("themeStore 暗色模式状态", () => {
  beforeEach(() => {
    localStorage.clear();
    useThemeStore.setState({ isDark: false });
  });

  it("默认亮色，toggle 后切换为暗色，再 toggle 恢复", () => {
    expect(useThemeStore.getState().isDark).toBe(false);

    useThemeStore.getState().toggleTheme();
    expect(useThemeStore.getState().isDark).toBe(true);

    useThemeStore.getState().toggleTheme();
    expect(useThemeStore.getState().isDark).toBe(false);
  });

  it("切换后写入 localStorage 持久化", () => {
    useThemeStore.getState().toggleTheme();

    const stored = JSON.parse(localStorage.getItem(THEME_STORAGE_KEY) ?? "{}");
    expect(stored.state?.isDark).toBe(true);
  });

  it("初始化时从 localStorage 恢复暗色偏好", async () => {
    localStorage.setItem(
      THEME_STORAGE_KEY,
      JSON.stringify({ state: { isDark: true }, version: 0 }),
    );
    await useThemeStore.persist.rehydrate();

    expect(useThemeStore.getState().isDark).toBe(true);
  });
});
