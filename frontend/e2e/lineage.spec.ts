import { expect, test } from "@playwright/test";
import { mockApi } from "./support/mockApi";

// 数据血缘可视化（Phase 2.3）：
// - mount 后自动拉取 /lineage/edges
// - 渲染 ECharts graph（真实 ECharts 渲染 <canvas>）
// - LayerFilter 默认 7 层全选；可取消勾选 SOURCE_SYSTEM 缩窄视图

test.describe("数据血缘可视化", () => {
  test("打开 /lineage 页面渲染 graph 节点", async ({ page }) => {
    await mockApi(page);
    await page.goto("/lineage");
    // Card 标题与菜单项都包含 "数据血缘"；用 .first() 避免严格模式冲突
    await expect(page.getByText("数据血缘").first()).toBeVisible();
    // 真实 ECharts 在生产环境渲染 <canvas>
    await expect(page.locator("canvas").first()).toBeVisible();
  });

  test("LayerFilter 取消勾选 SOURCE_SYSTEM 后过滤为 0 条边", async ({ page }) => {
    await mockApi(page);
    await page.goto("/lineage");
    await expect(page.locator("canvas").first()).toBeVisible();
    await page.getByLabel("SOURCE_SYSTEM").click();
    // mock 种子只有 SOURCE_SYSTEM → SOURCE_SYSTEM 与 SOURCE_SYSTEM → KPI 两类边；
    // 取消勾选 SOURCE_SYSTEM 后所有边都消失
    await expect(page.getByText(/当前筛选条件下无血缘边/)).toBeVisible();
  });
});
