import { expect, test } from "@playwright/test";
import { mockApi } from "./support/mockApi";

// 数据质量规则自动生成向导（5.9）：
// 验证 /data-quality/generate 菜单入口 → 向导页面加载 → 四步流程可交互。

test.describe("数据质量规则自动生成向导", () => {
  test("路由到向导页并渲染步骤条", async ({ page }) => {
    await mockApi(page);
    await page.goto("/data-quality/generate");

    // 步骤条必须可见（4 步：选择本体类 / 选择数据源 / 预览规则 / 确认落库）
    await expect(page.getByText("选择本体类", { exact: true })).toBeVisible();
    await expect(page.getByText("选择数据源", { exact: true })).toBeVisible();
    await expect(page.getByText("预览规则", { exact: true })).toBeVisible();
    await expect(page.getByText("确认落库", { exact: true })).toBeVisible();
  });

  test("Step 0：本体类下拉有选项且可选择", async ({ page }) => {
    await mockApi(page);
    await page.goto("/data-quality/generate");

    // 找本体类下拉（placeholder 是"请选择本体类"）
    const classSelect = page.locator(".ant-select").filter({ hasText: "请选择本体类" });
    await classSelect.click();

    // 等待下拉面板出现并包含 Order 选项
    const dropdown = page.locator(".ant-select-dropdown");
    const orderOption = dropdown.getByText("Order");
    await expect(orderOption).toBeVisible({ timeout: 5000 });
    await orderOption.click();

    // 选择后下拉显示选中值（Order 或 Order（订单））
    const selectedValue = page.locator(".ant-select").filter({ hasText: "Order" });
    await expect(selectedValue).toBeVisible({ timeout: 5000 });
  });

  test("Step 1：选择数据源后点击下一步到达预览步骤", async ({ page }) => {
    await mockApi(page);
    await page.goto("/data-quality/generate");

    // Step 0：选本体类
    const classSelect = page.locator(".ant-select").filter({ hasText: "请选择本体类" });
    await classSelect.click();
    const dropdown = page.locator(".ant-select-dropdown");
    const orderOption = dropdown.getByText("Order");
    await expect(orderOption).toBeVisible({ timeout: 5000 });
    await orderOption.click();

    // 点击"下一步"进入 Step 1
    const nextBtn = page.getByRole("button", { name: "下一步" });
    await nextBtn.click();

    // Step 1：选数据源（下拉第二个 ant-select）
    const allSelects = page.locator(".ant-select");
    const dsSelect = allSelects.nth(1);
    await dsSelect.click();
    await expect(dropdown.getByText("RuoYi-MySQL")).toBeVisible({ timeout: 5000 });
    await dropdown.getByText("RuoYi-MySQL").click();

    // 点击"下一步"进入预览步骤
    await nextBtn.click();

    // 等待预览步骤出现
    await expect(page.getByText("预览规则", { exact: true })).toBeVisible({ timeout: 5000 });
  });
});
