#!/usr/bin/env node
/**
 * 验证 wiki/chat 403 修复：登录 → 跳 /wiki-chat → 发问题 → 收 SSE token
 *
 * 用法（凭据只从环境变量来，脚本里不落任何口令）：
 *   cd frontend && E2E_USER=admin E2E_PASSWORD='<口令>' node ../scripts/e2e_smoke/wiki_chat_e2e.mjs
 */
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

mkdirSync("/tmp/e2e_smoke", { recursive: true });
const BASE = process.env.E2E_BASE_URL ?? "http://localhost:5173";
const USERNAME = process.env.E2E_USER ?? "admin";

// 口令**没有默认值**：写死一个等于把凭据提交进仓库
const PASSWORD = process.env.E2E_PASSWORD;
if (!PASSWORD) {
  console.error("缺少 E2E_PASSWORD —— 用法：E2E_USER=admin E2E_PASSWORD='<口令>' node ...");
  process.exit(1);
}

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await ctx.newPage();

const wikiCalls = [];
page.on("response", (r) => {
  if (r.url().includes("/api/v1/wiki/chat")) {
    wikiCalls.push({ status: r.status(), at: Date.now() });
  }
});
page.on("console", (m) => {
  if (m.type() === "error") console.error("  [browser]", m.text());
});

try {
  console.log("▶ 登录");
  await page.goto(`${BASE}/login`);
  await page.locator('input[autocomplete="username"]').fill(USERNAME);
  await page.locator('input[autocomplete="current-password"]').fill(PASSWORD);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 8000 });

  console.log("▶ 导航到 /wiki-chat");
  await page.goto(`${BASE}/wiki-chat`);
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: "/tmp/e2e_smoke/wiki_chat_loaded.png", fullPage: true });

  console.log("▶ 发送问题");
  // WikiChatPage 一般有 textarea + 发送按钮
  const textarea = page.locator("textarea").first();
  await textarea.fill("你好");
  await page.screenshot({ path: "/tmp/e2e_smoke/wiki_chat_filled.png", fullPage: true });
  await page.locator('button:has-text("发送")').first().click();

  console.log("▶ 等待 SSE 响应");
  const wikiResp = await page.waitForResponse(
    (r) => r.url().includes("/api/v1/wiki/chat") && r.request().method() === "POST",
    { timeout: 15000 },
  );
  const status = wikiResp.status();
  console.log(`  POST /api/v1/wiki/chat → ${status}`);

  if (status !== 200) {
    const body = await wikiResp.text().catch(() => "(no body)");
    throw new Error(`期望 200 实际 ${status}：${body.slice(0, 200)}`);
  }

  // 等流式内容出现
  await page.waitForTimeout(3000);
  await page.screenshot({ path: "/tmp/e2e_smoke/wiki_chat_response.png", fullPage: true });

  // 抓页面里 assistant 回复
  const assistantText = await page.evaluate(() => {
    const items = Array.from(document.querySelectorAll("[class*='assistant'], [class*='message']"));
    return items.map((el) => el.textContent?.trim()).filter((t) => t && t.length > 5).slice(-3);
  });

  console.log(`\n✅ HTTP 200, 期间 /wiki/chat 调用 ${wikiCalls.length} 次`);
  console.log("  assistant 文本片段：", JSON.stringify(assistantText));
} catch (e) {
  await page.screenshot({ path: "/tmp/e2e_smoke/wiki_chat_FAIL.png" }).catch(() => {});
  console.error("\n❌", e.message ?? e);
  console.log("  /wiki/chat 调用记录：", wikiCalls);
  process.exitCode = 1;
} finally {
  await browser.close();
}
