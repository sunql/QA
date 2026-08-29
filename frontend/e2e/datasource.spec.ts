import { expect, test } from "@playwright/test";
import { mockApi } from "./support/mockApi";

// 数据源 CRUD + 连接测试（5.9）：新增 → 列表展示；测试连接成功/失败；删除

test.describe("数据源管理", () => {
  test("新增数据源后列表展示新记录", async ({ page }) => {
    await mockApi(page);
    await page.goto("/datasource");

    await page.getByRole("button", { name: "新增数据源" }).click();
    await page.getByPlaceholder(/如 ZJTH-Oracle/).fill("Test-PG");
    await page.getByPlaceholder(/IP 或域名/).fill("127.0.0.1");
    await page.getByPlaceholder(/PG\/MySQL 填数据库名/).fill("test_db");
    await page.getByLabel("用户名").fill("pg_user");
    await page.getByLabel("密码").fill("secret");
    // antd Button 对两字中文自动插空格：可访问名为 "确 定"
    await page.getByRole("button", { name: /确\s*定/ }).click();

    await expect(page.getByText("创建成功")).toBeVisible();
    await expect(page.getByText("Test-PG")).toBeVisible();
    await expect(page.getByText("test_db")).toBeVisible();
  });

  test("连接测试：成功与失败提示", async ({ page }) => {
    await mockApi(page);
    await page.goto("/datasource");

    await page.getByRole("button", { name: "新增数据源" }).click();
    // 必填连接字段
    await page.getByPlaceholder(/如 ZJTH-Oracle/).fill("Conn-Test");
    const host = page.getByPlaceholder(/IP 或域名/);
    await host.fill("127.0.0.1");
    await page.getByPlaceholder(/PG\/MySQL 填数据库名/).fill("test_db");
    await page.getByLabel("用户名").fill("root");
    await page.getByLabel("密码").fill("secret");

    await page.getByRole("button", { name: "测试连接" }).click();
    await expect(page.getByText(/连接成功：连接成功（模拟）/)).toBeVisible();

    // 模拟后端对 10.255.255.254 返回失败
    await host.fill("10.255.255.254");
    await page.getByRole("button", { name: "测试连接" }).click();
    await expect(page.getByText(/连接失败：连接超时（模拟）/)).toBeVisible();
  });

  test("删除数据源后记录消失", async ({ page }) => {
    const backend = await mockApi(page);
    await page.goto("/datasource");

    await expect(page.getByText("ZJTH-Oracle")).toBeVisible();

    // 行定位用 .ant-table-row + hasText（getByRole('row',{name}) 对 Table 行无 accessible name）
    await page
      .locator(".ant-table-row", { hasText: "ZJTH-Oracle" })
      .getByRole("button", { name: /删\s*除/ })
      .click();
    await page.getByRole("button", { name: /确\s*定/ }).click();

    await expect(page.getByText("已删除")).toBeVisible();
    await expect(page.getByText("ZJTH-Oracle")).not.toBeVisible();
    expect(backend.datasources.some((d) => d.name === "ZJTH-Oracle")).toBe(false);
  });
});
