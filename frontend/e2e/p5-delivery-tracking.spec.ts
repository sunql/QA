/**
 * P5 到货跟踪 —— 真实栈视觉走查（不走 mockApi）。
 *
 * 与既有 e2e 的区别：既有 spec 用 `e2e/support/mockApi.ts` 在浏览器层拦截 /api/v1，
 * 这里**不拦截** —— 打到真实后端（localhost:8000 经 nginx 反代到 :5173），
 * 验证「登录 → 场景墙 → 到货跟踪 → 发消息 → 结构化卡片渲染」整条链。
 *
 * 跑法（frontend/ 下）：
 *   npx playwright test e2e/p5-delivery-tracking.spec.ts --config=e2e/p5.playwright.config.ts
 */

import { expect, test } from "@playwright/test";

/**
 * 本 spec 打**真实栈**（非 mockApi）⇒ 需要一个本地测试身份。
 *
 * 口令**从环境变量读**，不写进仓库：`P5_SMOKE_PASSWORD=... npx playwright test ...`
 * 真实栈不可达时**跳过**（而不是给出让人困惑的失败）—— CI 不跑 frontend e2e，
 * 本地没起栈时也不该红。
 */
const SMOKE_USER = process.env.P5_SMOKE_USER ?? "smoke_p4";
const SMOKE_PASSWORD = process.env.P5_SMOKE_PASSWORD ?? "";

/** 真实栈可达性探测（后端 auth 端点公开）。 */
async function isRealStackUp(): Promise<boolean> {
  try {
    const resp = await fetch("http://localhost:8000/api/v1/auth/password-policy");
    return resp.ok;
  } catch {
    return false;
  }
}

test("到货跟踪场景：登录 → 进场景 → 发消息 → 结构化卡片渲染", async ({ page }) => {
  test.skip(!SMOKE_PASSWORD, "未设 P5_SMOKE_PASSWORD —— 跳过（需要真实栈 + 测试身份）");
  test.skip(
    !(await isRealStackUp()),
    "真实栈不可达（backend :8000）—— 跳过（需先起本地栈）",
  );
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));

  // 1) 登录
  await page.goto("/login");
  // antd Form.Item name= 渲染成 id（#username/#password），无 placeholder/name
  await page.locator("#username").fill(SMOKE_USER);
  await page.locator("#password").fill(SMOKE_PASSWORD);
  // antd 两字按钮文本是「登 录」（中间有空格）⇒ 按 button 类型定位，不靠文案
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL((url) => !url.pathname.includes("/login"), { timeout: 30_000 });

  // 2) 进 Copilot 场景墙
  await page.goto("/procurement/copilot");
  await expect(
    page.getByText("到货跟踪", { exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });

  // 3) 进「到货跟踪」场景
  await page.getByText("到货跟踪", { exact: false }).first().click();

  // 3b) **进场景即出「本期业务现状」卡**（零 LLM：走 /delivery-tracking/overview，
  //     本期 = 企业 24 日结算周期）。这条卡不依赖用户发问，是 P5 的默认指标。
  await expect(
    page.getByText("本期业务现状（实时）", { exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/统计窗口/).first()).toBeVisible();
  await expect(page.getByText(/20\d\d-\d\d-\d\d ~ 20\d\d-\d\d-\d\d/).first()).toBeVisible();
  // 概览卡失败时的告警不应出现
  await expect(page.getByText(/统计加载失败|到货跟踪概览失败/)).toHaveCount(0);

  // 4) 用「近 3 个月到货概览」chip 发问（空消息 + 槽位预设）
  // ⚠ 用「近 3 个月」而不是「本期」：**同日复用**（同渲染文本 + 同 data_stamp ⇒ 复用旧 run，
  // 不重跑工具）会让本次走查复用**旧版本工具**产出的截断观测，测不到修复效果。
  const chip = page.getByText("近 3 个月到货概览");
  await expect(chip).toBeVisible({ timeout: 20_000 });
  await chip.click();

  // 5) 等结构化结果卡（真实编排要跑 LLM，耐心等）
  const card = page.getByText("到货达成概览", { exact: false }).first();
  await expect(card).toBeVisible({ timeout: 180_000 });

  // 6) 断言主指标与三态
  await expect(page.getByText(/订单到货率/).first()).toBeVisible();
  await expect(page.getByText(/量级加权到货率/).first()).toBeVisible();
  await expect(page.getByText(/全到货/).first()).toBeVisible();
  // 不应出现「结果数据过大」降级告警（观测未被截断）
  await expect(page.getByText(/结果数据过大/)).toHaveCount(0);

  // 7) 断言双 Tab
  // 文案在页面出现多次（tab label / 明细区）⇒ 用 .first() 避免 strict mode 多匹配
  await expect(page.getByText(/未到货订单/).first()).toBeVisible();
  await expect(page.getByText(/已到货订单/).first()).toBeVisible();

  // 8) 截图留证
  await page.screenshot({ path: "e2e/__screenshots__/p5-delivery-tracking.png", fullPage: true });

  // 9) 不应出现前端崩溃
  const fatal = errors.filter((e) => !/favicon|ResizeObserver|404 \(Not Found\)/i.test(e));
  expect(fatal, `前端控制台错误：\n${fatal.join("\n")}`).toHaveLength(0);
});

/**
 * 欢迎态专测：**进场景即出「本期业务现状」卡**，不发任何消息。
 *
 * 与上面的用例分开，是因为那条点在 chip 后就滚到结果卡了，
 * 欢迎态只在 `turns.length === 0` 时存在。
 */
test("到货跟踪欢迎态：进场景即出本期业务现状卡（零 LLM）", async ({ page }) => {
  test.skip(!SMOKE_PASSWORD, "未设 P5_SMOKE_PASSWORD —— 跳过（需要真实栈 + 测试身份）");
  test.skip(!(await isRealStackUp()), "真实栈不可达（backend :8000）—— 跳过");

  await page.goto("/login");
  await page.locator("#username").fill(SMOKE_USER);
  await page.locator("#password").fill(SMOKE_PASSWORD);
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL((url) => !url.pathname.includes("/login"), { timeout: 30_000 });

  await page.goto("/procurement/copilot");
  await page.getByText("到货跟踪", { exact: false }).first().click();

  const card = page.getByText("本期业务现状（实时）", { exact: false }).first();
  await expect(card).toBeVisible({ timeout: 30_000 });

  // 主指标 + 窗口 + 三态都在
  await expect(page.getByText("订单到货率").first()).toBeVisible();
  await expect(page.getByText("量级加权到货率").first()).toBeVisible();
  await expect(page.getByText(/统计窗口/).first()).toBeVisible();
  await expect(page.getByText(/20\d\d-\d\d-\d\d ~ 20\d\d-\d\d-\d\d/).first()).toBeVisible();
  // 不该走 LLM：此时不应有「分析结果」轮次
  await expect(page.getByText(/分析结果/)).toHaveCount(0);

  await page.screenshot({ path: "e2e/__screenshots__/p5-welcome-overview.png", fullPage: false });
});
