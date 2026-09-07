import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/tests/setup.ts"],
    // 排除 Playwright E2E（e2e/*.spec.ts），避免被 vitest 默认 include 捡入 jsdom 环境
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      thresholds: { lines: 80, functions: 80, branches: 80, statements: 80 },
      // 排除入口文件与 Playwright E2E 支撑代码（不可在 jsdom 单测中真实执行）
      exclude: [
        "e2e/**",
        "src/main.tsx",
        "src/tests/setup.ts",
        // i18n.ts 是 react-i18next 初始化模块，missingKeyHandler 是 init 回调，不需单测
        "src/i18n/i18n.ts",
        // types.ts 是纯类型文件（NestedKeyOf 条件类型），无运行时代码
        "src/i18n/types.ts",
        // src/types/** 是纯类型/枚举常量模块，无运行时代码
        "src/types/**",
      ],
    },
  },
});
