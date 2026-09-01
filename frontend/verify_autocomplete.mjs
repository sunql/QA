import { chromium } from "playwright";

const baseUrl = "http://localhost:5173";
const browser = await chromium.launch({ headless: true });

const ctx = await browser.newContext();
const page = await ctx.newPage();
const consoleErrors = [];
const failedRequests = [];
page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
page.on("requestfailed", (req) => failedRequests.push(`${req.method()} ${req.url()} -> ${req.failure()?.errorText}`));

await page.goto(`${baseUrl}/supplier-360`, { waitUntil: "networkidle", timeout: 30000 });
await page.waitForTimeout(1500);

// 页面顶部有语言选择器（也是 combobox），第 2 个才是我们的 AutoComplete
const allCombos = page.locator('input[role="combobox"]');
const acCount = await allCombos.count();
console.log(`Comboboxes on /supplier-360: ${acCount}`);
const acInput = allCombos.nth(acCount - 1);

// 类型 SUP，触发 debounce 搜索
await acInput.click();
await acInput.fill("SUP");
await page.waitForTimeout(700);  // > 300ms debounce + 网络

// 等下拉出现，检查企业编号命中
const dropdownVisible = await page.locator(".ant-select-item-option-content").count();
console.log(`Dropdown options visible: ${dropdownVisible}`);
const optionTexts = await page.locator(".ant-select-item-option-content").allTextContents();
console.log(`First option text: ${optionTexts[0]?.slice(0, 80)}`);

// 点击第一个命中
if (dropdownVisible > 0) {
  await page.locator(".ant-select-item-option").first().click();
  await page.waitForTimeout(300);
  const inputVal = await acInput.inputValue();
  console.log(`After select, input value: ${inputVal}`);
}

// 点查询按钮 → 应触发 360° 拉取
const queryBtn = page.locator('button:has-text("查询")').first();
await queryBtn.click();
await page.waitForTimeout(2000);

const has360Card = await page.locator("text=企业编码").or(page.locator("text=主数据")).count();
console.log(`360° card sections visible: ${has360Card}`);
const bodyText = (await page.locator("body").innerText()).slice(0, 300).replace(/\s+/g, " ");
console.log(`Body sample: ${bodyText}`);

console.log(`consoleErrors: ${consoleErrors.length === 0 ? "none" : JSON.stringify(consoleErrors)}`);
console.log(`failedRequests: ${failedRequests.length === 0 ? "none" : JSON.stringify(failedRequests)}`);

await ctx.close();
await browser.close();