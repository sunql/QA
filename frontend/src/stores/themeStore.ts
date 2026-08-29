import { create } from "zustand";
import { persist } from "zustand/middleware";

// 暗色模式本地存储 key
export const THEME_STORAGE_KEY = "qa-system-theme";

interface ThemeState {
  isDark: boolean;
  toggleTheme: () => void;
}

// 暗色模式主题 store（5.8）：isDark + toggleTheme，persist 中间件持久化到 localStorage。
// 切换主题是不可变更新（返回新状态对象），符合全局编码规范。
export const useThemeStore = create<ThemeState>()(
  persist(
    (set) => ({
      isDark: false,
      toggleTheme: () => set((state) => ({ isDark: !state.isDark })),
    }),
    { name: THEME_STORAGE_KEY },
  ),
);
