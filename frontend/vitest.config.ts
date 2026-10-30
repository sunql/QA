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
    // antd 重组件（Table/Modal/Select）在 jsdom + 并行 worker 下渲染耗时波动大，
    // 默认 5s 在全量跑时常被负载顶穿（单文件跑全绿、全量跑随机超时）→ 提到 15s
    testTimeout: 15_000,
    hookTimeout: 10_000,
    // 排除 Playwright E2E（e2e/*.spec.ts），避免被 vitest 默认 include 捡入 jsdom 环境
    // 排除 .worktrees/（本地 git worktree mount，与本目录测试完全无关）
    exclude: ["e2e/**", "node_modules/**", "dist/**", ".worktrees/**"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "json-summary"],
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
        // 测试文件自身不进覆盖率分母：它们的函数覆盖率天然偏高
        // （2026-10-30 实测 321/381 = 84.25%，而产品代码 src/** 只有 1419/2015 =
        // 70.42%）⇒ 计入分母会把全局数字抬高 2 个点，属于自欺。
        // 口径变更后基线：产品代码 functions 70.42%（达标 80% 需覆盖 1612 个）。
        "src/tests/**",
        "**/__tests__/**",
        // 根级构建/校验支撑文件，不是产品代码（vite/vitest/playwright 配置 +
        // verify_*.mjs 人工校验脚本）
        "*.config.ts",
        "*.config.js",
        "*.config.d.ts",
        "verify_*.mjs",
      ],
    },
  },
});
