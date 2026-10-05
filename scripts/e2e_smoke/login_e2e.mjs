#!/usr/bin/env node
/**
 * 端到端冒烟：前端 LoginPage → 后端 auth → 拿 token
 *
 * 真实链路，不 mock：
 *   1. 浏览器访问 http://localhost:5173/login
 *   2. 用环境变量给的账号口令提交
 *   3. 验证：localStorage 'qa-system-auth' 出现 accessToken
 *   4. 验证：页面跳转到 /（不是 /login）
 *   5. 验证：调 /api/v1/auth/me 拿到的 user.username 与登录账号一致
 *
 * 用法（凭据只从环境变量来，脚本里不落任何口令）：
 *   cd frontend && E2E_USER=admin E2E_PASSWORD='<口令>' node ../scripts/e2e_smoke/login_e2e.mjs
 */

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = "/tmp/e2e_smoke";
mkdirSync(OUT_DIR, { recursive: true });

const BASE_URL = process.env.E2E_BASE_URL ?? "http://localhost:5173";
const USERNAME = process.env.E2E_USER ?? "admin";

// 口令**没有默认值**：这是真实 auth 的线上栈，写死一个口令等于把凭据提交进仓库。
// 缺了就 fail fast，别让脚本用一个猜的口令去撞登录接口。
const PASSWORD = process.env.E2E_PASSWORD;
if (!PASSWORD) {
  console.error("缺少 E2E_PASSWORD —— 用法：E2E_USER=admin E2E_PASSWORD='<口令>' node ...");
  process.exit(1);
}

function step(name, fn) {
  return (async () => {
    process.stdout.write(`▶ ${name} ... `);
    try {
      const out = await fn();
      console.log("✅", out ?? "");
      return true;
    } catch (e) {
      console.log("❌", e.message ?? e);
      throw e;
    }
  })();
}

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await ctx.newPage();

// 抓所有 /api/v1 请求/响应，便于排错
const apiCalls = [];
page.on("response", async (resp) => {
  const url = resp.url();
  if (url.includes("/api/v1/")) {
    apiCalls.push({ url: url.replace(BASE_URL, ""), status: resp.status() });
  }
});
page.on("console", (msg) => {
  if (msg.type() === "error") console.error("  [browser console.error]", msg.text());
});
page.on("pageerror", (err) => console.error("  [browser pageerror]", err.message));

try {
  await step("1. 打开 /login", async () => {
    await page.goto(`${BASE_URL}/login`, { waitUntil: "networkidle" });
    await page.screenshot({ path: `${OUT_DIR}/01_login_page.png`, fullPage: true });
    return "已截图 01_login_page.png";
  });

  await step("2. 填表 + 提交", async () => {
    await page.locator('input[autocomplete="username"]').fill(USERNAME);
    await page.locator('input[autocomplete="current-password"]').fill(PASSWORD);
    await page.screenshot({ path: `${OUT_DIR}/02_form_filled.png`, fullPage: true });
    await page.locator('button[type="submit"]').click();
    // 等 token 写入 localStorage 或网络请求完成
    await page.waitForResponse((r) => r.url().includes("/api/v1/auth/login") && r.status() === 200, { timeout: 10_000 });
    return "登录接口返回 200";
  });

  await step("3. localStorage 出现 token", async () => {
    const raw = await page.evaluate(() => window.localStorage.getItem("qa-system-auth"));
    if (!raw) throw new Error("localStorage 'qa-system-auth' 不存在");
    const parsed = JSON.parse(raw);
    if (!parsed?.state?.token) throw new Error("token 字段为空: " + raw);
    const len = parsed.state.token.length;
    if (len < 100) throw new Error(`token 长度异常 ${len}`);
    return `token 长度 ${len}, prefix=${parsed.state.token.slice(0, 20)}…`;
  });

  await step("4. 跳转到 /", async () => {
    await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 5_000 });
    await page.waitForLoadState("networkidle");
    const finalUrl = page.url();
    await page.screenshot({ path: `${OUT_DIR}/03_after_login.png`, fullPage: true });
    return `当前 URL = ${finalUrl}`;
  });

  await step("5. 调 /auth/me 校验 token 真实可用", async () => {
    const me = await page.evaluate(async () => {
      const raw = window.localStorage.getItem("qa-system-auth");
      const { token } = JSON.parse(raw).state;
      const resp = await fetch("/api/v1/auth/me", { headers: { Authorization: `Bearer ${token}` } });
      return { status: resp.status, body: resp.ok ? await resp.json() : await resp.text() };
    });
    if (me.status !== 200) throw new Error(`/auth/me 返回 ${me.status}: ${JSON.stringify(me.body).slice(0, 100)}`);
    if (me.body.username !== USERNAME) throw new Error(`username 异常: ${me.body.username}`);
    return `user.username=${me.body.username}, roles=${JSON.stringify(me.body.roles)}`;
  });

  await step("6. 用 token 调 admin-only /users", async () => {
    const resp = await page.evaluate(async () => {
      const raw = window.localStorage.getItem("qa-system-auth");
      const { token } = JSON.parse(raw).state;
      const r = await fetch("/api/v1/users", { headers: { Authorization: `Bearer ${token}` } });
      return { status: r.status, count: r.ok ? (await r.json()).length ?? (await r.json())?.items?.length ?? "?" : null };
    });
    if (resp.status !== 200) throw new Error(`/users 返回 ${resp.status}`);
    return `HTTP 200, users 列表可访问`;
  });

  console.log("\n=== 期间 API 调用 ===");
  for (const c of apiCalls) console.log(`  ${c.status}  ${c.url}`);
  console.log(`\n✅ 全部 6 步通过。截图在 ${OUT_DIR}/`);
} catch (e) {
  await page.screenshot({ path: `${OUT_DIR}/FAIL_${Date.now()}.png`, fullPage: true }).catch(() => {});
  console.error("\n=== 期间 API 调用 ===");
  for (const c of apiCalls) console.log(`  ${c.status}  ${c.url}`);
  console.error("\n❌ 冒烟失败:", e.message ?? e);
  process.exitCode = 1;
} finally {
  await browser.close();
}
