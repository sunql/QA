/**
 * 通用 Agent 编排 E2E（general agent orchestration）。
 *
 * 覆盖两条主链路：
 *  1. 编排配置页 /agent-orchestrations：列表渲染 → 新建（默认停用）→ 绑定工具 → 启用。
 *  2. 任务页 /agent-tasks：创建运行 → 暂停（补充输入）→ 恢复 → 查看轨迹与最终结论；
 *     另有一条运行中的运行用于验证「取消运行」。
 *
 * 注意：
 *  - antd Button 恰好两汉字会自动插 U+0020（「创 建」非「创建」），按钮断言用正则。
 *  - mockApi 拦截 /api/v1/**，并按 agentCode 约定运行初始状态：
 *    demo-agent → PAUSED（等待补充输入），runner-agent → RUNNING（可取消）。
 *  - 必须先种假 token，否则 RequireAuth 会把页面弹到 /login。
 */

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { mockApi } from "./support/mockApi";

const FAKE_TOKEN = "fake-token-for-e2e-mock";
const AUTH_STORAGE_KEY = "qa-system-auth";

/** 种一个假登录态：mockApi 不接 /auth/me，非空 token 即可通过 RequireAuth。 */
async function loginAsMock(page: Page): Promise<void> {
  await page.addInitScript((data: { key: string; value: string }) => {
    window.localStorage.setItem(data.key, data.value);
  }, {
    key: AUTH_STORAGE_KEY,
    value: JSON.stringify({
      state: {
        token: FAKE_TOKEN,
        user: null,
        mustChangePassword: false,
        rememberMe: false,
      },
      version: 0,
    }),
  });
}

/**
 * 打开下拉并依次选中若干选项（按 placeholder 文案定位下拉框）。
 *
 * 多选下拉选中后不会自动收起，所以一次调用里选完所有选项，
 * 避免二次点击同一个下拉框把它切换成收起状态。
 *
 * 下拉面板收起时 antd 会加 ant-slide-up-leave 动画类，此时它仍然“可见”，
 * 会让全局下拉定位器同时命中两个面板；这里排除正在收起的面板，
 * 并靠选项文案唯一性（各下拉的选项互不相同）避免 strict mode 冲突。
 */
async function pickOptions(
  page: Page,
  selectText: string,
  optionTexts: string[],
): Promise<void> {
  await page.locator(".ant-select").filter({ hasText: selectText }).click();
  for (const optionText of optionTexts) {
    const option = page.locator(
      ".ant-select-dropdown:not(.ant-slide-up-leave) .ant-select-item-option",
      { hasText: optionText },
    );
    await expect(option).toBeVisible();
    await option.click();
  }
}

test.describe("Agent 编排配置页", () => {
  test("列出种子配置并展示启用状态与绑定工具数", async ({ page }) => {
    await loginAsMock(page);
    await mockApi(page);
    await page.goto("/agent-orchestrations");

    await expect(page.getByRole("heading", { name: "Agent 编排配置" })).toBeVisible();

    const demoRow = page.locator("tr", { hasText: "demo-agent" });
    await expect(demoRow).toBeVisible();
    await expect(demoRow.getByText("已启用")).toBeVisible();
    await expect(demoRow.getByText("已绑定 1 个工具")).toBeVisible();
  });

  test("新建配置默认停用，绑定两个工具后显式启用", async ({ page }) => {
    await loginAsMock(page);
    await mockApi(page);
    await page.goto("/agent-orchestrations");

    await page.getByRole("button", { name: "新建配置" }).click();
    await expect(page.getByText("新建编排配置")).toBeVisible();

    // Agent 编码：draft-agent 尚无编排配置
    await pickOptions(page, "选择 Agent", ["draft-agent"]);
    await page
      .getByPlaceholder("描述该 Agent 的编排策略与约束")
      .fill("草稿 Agent 的编排策略：只读查询，禁止写操作。");
    await pickOptions(page, "选择该 Agent 可调用的只读工具", [
      "tool-alpha",
      "tool-beta",
    ]);
    // 多选下拉仍展开着，点一下弹窗标题把它收起，避免挡住确定按钮。
    await page.getByText("新建编排配置").click();

    await page.getByRole("button", { name: /创\s*建/ }).click();

    const draftRow = page.locator("tr", { hasText: "draft-agent" });
    await expect(draftRow).toBeVisible();
    // 新建默认停用
    await expect(draftRow.getByText("未启用")).toBeVisible();
    await expect(draftRow.getByText("已绑定 2 个工具")).toBeVisible();

    // 显式切换启用
    await draftRow.getByRole("switch").click();
    await expect(draftRow.getByText("已启用")).toBeVisible();
  });
});

test.describe("Agent 任务页", () => {
  test("创建运行 → 暂停补充输入 → 恢复后看到轨迹与最终结论", async ({ page }) => {
    await loginAsMock(page);
    await mockApi(page);
    await page.goto("/agent-tasks");

    await expect(page.getByRole("heading", { name: "Agent 任务" })).toBeVisible();

    await pickOptions(page, "选择已启用的编排配置", ["demo-agent"]);
    await pickOptions(page, "选择已启用状态的模型", ["mock-model"]);
    await page
      .getByRole("textbox", { name: "任务输入" })
      .fill("查询供应商 10105 的收货数量");
    await page.getByRole("button", { name: "创建运行" }).click();

    // 运行暂停在「需要补充输入」
    await expect(page.getByText("运行 ID：run-1")).toBeVisible();
    await expect(page.getByText("已暂停")).toBeVisible();
    await expect(page.getByText("需要补充输入")).toBeVisible();

    await page
      .getByRole("textbox", { name: "补充输入" })
      .fill("供应商编码 10105");
    await page.getByRole("button", { name: "恢复运行" }).click();

    // 恢复后运行成功：轨迹补两步工具调用，并给出最终结论
    await expect(page.getByText("已成功")).toBeVisible();
    await expect(page.getByTestId("agent-run-step-2")).toBeVisible();
    await expect(page.getByTestId("agent-run-step-3")).toBeVisible();
    await expect(
      page.getByText("已完成供应商收货数量查询，共 2 条记录。"),
    ).toBeVisible();
  });

  test("运行中的运行可以取消", async ({ page }) => {
    await loginAsMock(page);
    await mockApi(page);
    await page.goto("/agent-tasks");

    await pickOptions(page, "选择已启用的编排配置", ["runner-agent"]);
    await pickOptions(page, "选择已启用状态的模型", ["mock-model"]);
    await page
      .getByRole("textbox", { name: "任务输入" })
      .fill("运行中的任务：统计本月收货数量");
    await page.getByRole("button", { name: "创建运行" }).click();

    await expect(page.getByText("运行中")).toBeVisible();
    await page.getByRole("button", { name: "取消运行" }).click();
    await expect(page.getByText("已取消")).toBeVisible();
  });
});
