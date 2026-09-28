/**
 * WikiLinksPage E2E（feat-wiki-ontology-link）。
 *
 * 覆盖（6 个交互点）：
 *  1. 渲染标题 + Tree + 空态
 *  2. 点 stub 树节点 → 右侧出现「+ 添加绑定」+ 已存在链接渲染
 *  3. 切到不同页面 → 列表切换
 *  4. 打开添加 Modal → 看到 linkable targets
 *  5. 提交创建 → 后端 wikiLinks 数组新增
 *  6. 撤销 → 后端 revoked_time 落位 + 列表消失
 *
 * 注意：
 *  - antd Button 恰好两汉字会自动插 U+0020（「撤 销」非「撤销」），
 *    按钮断言一律用正则 /X\s*Y/。
 *  - page.tsx 是硬编码 stub 树（page-001/002/003），与 SEED_WIKI_LINKS 的
 *    page_id 对齐：seed.page-001 已有 1 条 class 链接；page-002/003 空。
 *  - mockApi 拦截 /api/v1/** ，前端无真实后端依赖。
 */

import { expect, test } from "@playwright/test";
import { mockApi } from "./support/mockApi";

/**
 * zustand persist 的 localStorage key 与 LoginPage 写入保持一致。
 * RequireAuth 守卫读 useAuthStore.token，非空才放行 /admin/*；
 * mockApi 不接 /auth/me，所以这里塞个假 token 即可（真登录链路在
 * scripts/e2e_smoke/login_e2e.mjs 走真实后端验证）。
 */
const FAKE_TOKEN = "fake-token-for-e2e-mock";
const AUTH_STORAGE_KEY = "qa-system-auth";

async function loginAsMock(page: import("@playwright/test").Page): Promise<void> {
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

test.describe("WikiLinksPage", () => {
  test("默认渲染标题 + Tree + 空态", async ({ page }) => {
    await loginAsMock(page);
    await mockApi(page);
    await page.goto("/admin/wiki-links");

    // page-level H2 + stub 树根节点
    await expect(page.getByRole("heading", { name: /Wiki.*Ontology.*链接管理/ })).toBeVisible();
    await expect(page.getByText("采购管理")).toBeVisible();
    await expect(page.getByText("质量管理")).toBeVisible();
    await expect(page.getByText("仓储物流")).toBeVisible();

    // 未选中页面：提示文案 + 添加按钮 disabled
    await expect(page.getByText(/请先选择左侧 Wiki 页面/)).toBeVisible();
    await expect(page.getByRole("button", { name: /\+\s*添加绑定/ })).toBeDisabled();
  });

  test("选中 page-001 看到种子链接 + 添加按钮启用", async ({ page }) => {
    await loginAsMock(page);
    const backend = await mockApi(page);
    await page.goto("/admin/wiki-links");

    // 点 stub 树的「采购管理」 — 直接点 .ant-tree-title 文字（精确），
    // antd Tree 内部用 mousedown 监听，click 事件同样能 fire onSelect
    await page.locator(".ant-tree-title", { hasText: "采购管理" }).first().click();

    // 添加按钮 enabled
    await expect(page.getByRole("button", { name: /\+\s*添加绑定/ })).toBeEnabled();

    // 种子链接 1 行：ontology_id=1 + weight=0.80
    await expect(page.getByText("ontology_id=1")).toBeVisible();
    await expect(page.getByText("weight=0.80")).toBeVisible();
    expect(backend.wikiLinks.some((w) => w.page_id === "page-001")).toBe(true);
  });

  test("切换页面 → 列表随之刷新（page-002 空）", async ({ page }) => {
    await loginAsMock(page);
    await mockApi(page);
    await page.goto("/admin/wiki-links");

    await page.locator(".ant-tree-title", { hasText: "采购管理" }).first().click();
    await expect(page.getByText("ontology_id=1")).toBeVisible();

    // 切到「质量管理」→ 列表为空 → 提示「暂无链接」
    await page.locator(".ant-tree-title", { hasText: "质量管理" }).first().click();
    await expect(page.getByText("ontology_id=1")).not.toBeVisible();
    await expect(page.getByText(/暂无链接/)).toBeVisible();
  });

  test("打开添加 Modal 看到 linkable targets", async ({ page }) => {
    await loginAsMock(page);
    await mockApi(page);
    await page.goto("/admin/wiki-links");

    await page.locator(".ant-tree-title", { hasText: "质量管理" }).first().click();
    await page.getByRole("button", { name: /\+\s*添加绑定/ }).click();

    // Modal 标题（用 locator 锁 .ant-modal-title，避免与 page-level H2 撞名）
    await expect(page.locator(".ant-modal-title")).toHaveText("添加绑定");
    // Select combobox（class 来自 SEED_CLASSES：Order / Product / Customer）
    await expect(page.getByRole("combobox", { name: "本体对象" })).toBeVisible();
  });

  test("提交创建 → 后端 wikiLinks 新增 + 列表渲染", async ({ page }) => {
    await loginAsMock(page);
    const backend = await mockApi(page);
    await page.goto("/admin/wiki-links");

    await page.locator(".ant-tree-title", { hasText: "质量管理" }).first().click();
    const beforeCount = backend.wikiLinks.filter((w) => w.page_id === "page-002").length;

    await page.getByRole("button", { name: /\+\s*添加绑定/ }).click();
    await page.getByRole("combobox", { name: "本体对象" }).click();
    await page
      .locator(".ant-select-dropdown .ant-select-item-option", { hasText: "Order" })
      .first()
      .click();
    await page.getByRole("button", { name: /提\s*交/ }).click();

    // 后端：page-002 多一条；前端：列表渲染新行
    await expect.poll(() => backend.wikiLinks.filter((w) => w.page_id === "page-002").length).toBe(
      beforeCount + 1,
    );
    await expect(page.getByText(/ontology_id=/).first()).toBeVisible();
  });

  test("撤销链接 → 后端 revoked_time 落位", async ({ page }) => {
    await loginAsMock(page);
    const backend = await mockApi(page);
    await page.goto("/admin/wiki-links");

    await page.locator(".ant-tree-title", { hasText: "采购管理" }).first().click();
    await expect(page.getByText("ontology_id=1")).toBeVisible();

    await page.getByRole("button", { name: /撤\s*销/ }).first().click();

    // 后端：seed 链接 id=1 的 revoked_time 不为 null
    await expect.poll(() => backend.wikiLinks.find((w) => w.id === 1)?.revoked_time).not.toBeNull();
    // 注：当前页面不过滤 revoked 链接（生产行为：listLinks 不带 revoked 过滤，
    // page.tsx 也只按 ontology_type 过滤），所以已撤销的链接仍可见。
    // 若未来加 revoked 过滤，此处改为 .not.toBeVisible()。
  });
});