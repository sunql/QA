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

// 类型 10105（THBI 真实供应商编码）
await acInput.click();
await acInput.fill("10105");
await page.waitForTimeout(700);

const optCount = await page.locator(".ant-select-item-option-content").count();
const optText = (await page.locator(".ant-select-item-option-content").first().textContent()) || "";
console.log(`dropdown hits: ${optCount}`);
console.log(`first option: ${optText.replace(/\s+/g, " ").slice(0, 80)}`);

await ctx.close();
await browser.close();