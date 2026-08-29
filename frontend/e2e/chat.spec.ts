import { expect, test } from "@playwright/test";
import { mockApi } from "./support/mockApi";

// 聊天主流程（5.9）：默认数据源自动选中 → SSE 流式回答 → SQL 预览 / 图表 / token 计量

test.describe("聊天流程", () => {
  test("闲聊快速路径：返回问候且不渲染 SQL/图表", async ({ page }) => {
    await mockApi(page);
    await page.goto("/chat");

    const input = page.getByPlaceholder(/输入自然语言问题/);
    await input.fill("你好");
    await input.press("Enter");

    await expect(page.getByText(/您好，我是智能问答助手/)).toBeVisible();
    await expect(page.getByText("查看 SQL")).not.toBeVisible();
    await expect(page.locator("canvas").first()).not.toBeVisible();
    // done 事件携带 token 计量 → 消息尾展示 Tokens 标签
    await expect(page.getByText(/Tokens: 0/)).toBeVisible();
  });

  test("查询流程：SSE 逐块渲染回答 + SQL 预览 + 图表 + token 计量", async ({ page }) => {
    await mockApi(page);
    await page.goto("/chat");

    // ChatPanel 自动选中 isDefault 数据源（RuoYi-MySQL），无需手动选择
    await expect(page.getByText("RuoYi-MySQL")).toBeVisible();

    const input = page.getByPlaceholder(/输入自然语言问题/);
    await input.fill("各供应商的收货数量汇总");
    await input.press("Enter");

    // 流式 token 累加后的完整回答
    await expect(page.getByText(/已为您查询各供应商收货数量，共 2 条记录/)).toBeVisible();
    // SQL 预览：折叠 → 展开后可见 SQL 文本
    await expect(page.getByText("查看 SQL")).toBeVisible();
    await page.getByText("查看 SQL").click();
    await expect(page.getByText(/SELECT s\.supplier, SUM\(r\.qty\)/)).toBeVisible();
    // 柱状图：echarts-for-react 渲染 canvas
    await expect(page.locator("canvas").first()).toBeVisible();
    // done 事件 token/cost/模型名 计量
    await expect(page.getByText(/Tokens: 42/)).toBeVisible();
    await expect(page.getByText(/成本: \$0\.000200/)).toBeVisible();
    await expect(page.getByText(/模型: mock-model/)).toBeVisible();
  });

  test("空输入不发送：点击发送不产生消息", async ({ page }) => {
    await mockApi(page);
    await page.goto("/chat");

    await page.getByRole("button", { name: /发送/ }).click();
    // 列表仍为空态
    await expect(page.getByText("输入问题开始对话")).toBeVisible();
  });
});
