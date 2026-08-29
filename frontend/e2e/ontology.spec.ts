import { expect, test } from "@playwright/test";
import { mockApi } from "./support/mockApi";

// 本体管理（5.9）：创建类 → 属性标签页添加属性；编辑类；删除类
// 注意：antd Button 对恰好两个汉字自动插空格（如 "确 定" / "编 辑"），
// 断言按钮一律用正则 /X\s*Y/；getByText 是子串且大小写不敏感，
// 断言单元格用 getByRole("cell", { name, exact: true }) 避免误配。

test.describe("本体管理", () => {
  test("创建本体类并为其添加属性", async ({ page }) => {
    await mockApi(page);
    await page.goto("/ontology");

    // ---- 类 ----
    await page.getByRole("button", { name: "新增类" }).click();
    await page.getByPlaceholder(/如 Customer/).fill("Product");
    await page.getByPlaceholder(/如 客户/).fill("商品");
    await page.getByPlaceholder(/如 t_customer/).fill("t_product");
    await page.getByPlaceholder("类用途说明").fill("商品实体");
    await page.getByRole("button", { name: /确\s*定/ }).click();

    await expect(page.getByText("创建成功")).toBeVisible();
    await expect(page.getByRole("cell", { name: "Product", exact: true })).toBeVisible();
    await expect(page.getByRole("cell", { name: "t_product", exact: true })).toBeVisible();

    // ---- 属性 ----
    await page.getByRole("tab", { name: "属性" }).click();
    await page.getByRole("button", { name: "新增属性" }).click();
    // 所属类下拉选择刚创建的 Product（选项 label 含别名："Product (商品)"）。
    // 直接点 combobox input 会被默认值 "0" 的 selection-item span 拦截，改点
    // .ant-select-selector 打开下拉；选项定位用 antd 下拉容器（body portal，
    // getByRole('option') 会命中隐藏重复渲染导致"not visible"）。
    await page.getByRole("dialog").locator(".ant-select-selector").first().click();
    await page.locator(".ant-select-dropdown .ant-select-item-option", { hasText: "Product" }).click();
    await page.getByPlaceholder(/customer_name/).fill("product_code");
    await page.getByPlaceholder(/订单ID/).fill("商品编码");
    await page.getByRole("button", { name: /确\s*定/ }).click();

    // 类创建与属性创建的 "创建成功" toast 可能同时在屏，取 first 即可
    await expect(page.getByText("创建成功").first()).toBeVisible();
    await expect(page.getByRole("cell", { name: "product_code", exact: true })).toBeVisible();
    await expect(page.getByRole("cell", { name: "Product", exact: true })).toBeVisible();
  });

  test("编辑类名生效", async ({ page }) => {
    await mockApi(page);
    await page.goto("/ontology");

    await page
      .locator(".ant-table-row", { hasText: "Order" })
      .getByRole("button", { name: /编\s*辑/ })
      .click();
    await page.getByPlaceholder(/如 Customer/).fill("OrderV2");
    await page.getByRole("button", { name: /确\s*定/ }).click();

    await expect(page.getByText("更新成功")).toBeVisible();
    await expect(page.getByRole("cell", { name: "OrderV2", exact: true })).toBeVisible();
    await expect(page.getByRole("cell", { name: "Order", exact: true })).not.toBeVisible();
  });

  test("删除类后记录消失", async ({ page }) => {
    const backend = await mockApi(page);
    await page.goto("/ontology");

    await expect(page.getByRole("cell", { name: "Order", exact: true })).toBeVisible();
    await page
      .locator(".ant-table-row", { hasText: "Order" })
      .getByRole("button", { name: /删\s*除/ })
      .click();
    await page.getByRole("button", { name: /确\s*定/ }).click();

    await expect(page.getByText("已删除")).toBeVisible();
    await expect(page.getByRole("cell", { name: "Order", exact: true })).not.toBeVisible();
    expect(backend.classes.some((c) => c.className === "Order")).toBe(false);
  });
});
