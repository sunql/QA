import { expect, test } from "@playwright/test";
import { mockApi } from "./support/mockApi";

// 暗色模式（5.9）：切换开关 → antd darkAlgorithm 生效（Header 背景变化）+ localStorage 持久化

function headerBg(page: import("@playwright/test").Page): Promise<string> {
  // L4 修复：布局内可能存在多层 header（antd 内部组件），断言取首个布局 header
  return page.locator("header").first().evaluate((el) => getComputedStyle(el).backgroundColor);
}

test.describe("暗色模式", () => {
  test("切换开关进入暗色模式并持久化到 localStorage", async ({ page }) => {
    await mockApi(page);
    await page.goto("/models");

    const lightBg = await headerBg(page);
    expect(lightBg).toBeTruthy();

    const toggle = page.getByRole("switch");
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await toggle.click();

    // 背景随 colorBgContainer token 变化（亮 → 暗），不绑定 antd 推导色值
    const darkBg = await headerBg(page);
    expect(darkBg).not.toBe(lightBg);
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    // localStorage 持久化：zustand persist 存储 {state:{isDark:true}, version:0}
    const stored = await page.evaluate(() =>
      JSON.parse(localStorage.getItem("qa-system-theme") ?? "{}")
    );
    expect(stored.state?.isDark).toBe(true);
  });

  test("刷新页面后暗色模式保持（localStorage 恢复）", async ({ page }) => {
    await mockApi(page);
    await page.goto("/models");

    const lightBg = await headerBg(page);
    await page.getByRole("switch").click();
    const darkBg = await headerBg(page);
    expect(darkBg).not.toBe(lightBg);

    await page.reload();
    await expect(page.getByRole("switch")).toHaveAttribute("aria-checked", "true");
    // M2 修复：不绑定 reload 后 antd 对同一暗色 token 的推导色值，只断言"仍处于暗色"（≠ 亮色）
    expect(await headerBg(page)).not.toBe(lightBg);
  });
});
