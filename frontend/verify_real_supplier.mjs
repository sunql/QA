import { chromium } from "playwright";

const baseUrl = "http://localhost:5173";
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext();
const page = await ctx.newPage();

await page.goto(`${baseUrl}/supplier-360`, { waitUntil: "networkidle", timeout: 30000 });
await page.waitForTimeout(1500);

// 第二个 combobox 是 EntityAutoComplete（第一个是页面顶部语言选择器）
const allCombos = page.locator('input[role="combobox"]');
const acInput = allCombos.nth(await allCombos.count() - 1);

// 类型 A（1 字符 — 验证去 2 字符门槛 + name 展示）
await acInput.click();
await acInput.fill("A");
await page.waitForTimeout(700);

const optCount = await page.locator(".ant-select-item-option-content").count();
const optTexts = await page.locator(".ant-select-item-option-content").allTextContents();
console.log(`dropdown hits: ${optCount}`);
for (let i = 0; i < Math.min(3, optTexts.length); i++) {
  console.log(`  [${i}] ${optTexts[i].replace(/\s+/g, " ").slice(0, 90)}`);
}

await ctx.close();
await browser.close();