/**
 * P5 视觉走查专用 Playwright 配置（2026-11-26）。
 *
 * 与既有 `playwright.config.ts` 的差别：
 * - **不启 webServer** —— 走真实栈（前端 nginx 容器 :5173 + 后端 :8000）；
 * - baseURL 指向真实容器端口；
 * - 单 worker（避免多个 spec 抢同一个真实会话）。
 */

import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./",
  testMatch: /p5-delivery-tracking\.spec\.ts/,
  timeout: 300_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5173",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
});
