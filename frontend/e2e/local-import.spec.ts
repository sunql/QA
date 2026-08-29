import { expect, test } from "@playwright/test";
import { mockApi } from "./support/mockApi";

test.describe("本地导入", () => {
  test("本地导入向导全流程", async ({ page }) => {
    await mockApi(page);
    await page.goto("/datasource");

    await page.getByRole("button", { name: "智能导入到本体" }).first().click();
    await expect(page.getByText("规则配置")).toBeVisible();
    await page.getByRole("button", { name: "下一步" }).click();
    // 预览加载完成后出现确认导入按钮（Step1 内容渲染的唯一信号）
    await expect(page.getByRole("button", { name: "确认导入" })).toBeVisible();
    await page.getByRole("button", { name: "确认导入" }).click();
    await expect(page.locator(".ant-result-title")).toHaveText("导入完成");
  });
});
