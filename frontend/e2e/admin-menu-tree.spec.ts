/** 菜单管理页（feat-menu-tree）E2E：走真实后端（mockApi 不拦截 /api/v1）。
 *
 * 验证目标：
 *  1. /admin/menus 默认进入「树形视图」Tab
 *  2. antd Tree 渲染至少 1 个一级类 + 1 个叶子项
 *  3. 切到「列表视图」Tab → Table 渲染含 sortOrder / visible 列
 *  4. 改 sortOrder 走 PUT → 列表视图可见新值 + 还原
 *  5. 叶子项切 parentCode 走 PUT → 还原
 *  6. 叶子项拖到非 section（叶子）→ 后端 422 + parent 未变
 *
 * 注：本 spec 故意不调用 mockApi()，让所有 /api/v1 请求穿透到真实后端
 * （依赖 vite proxy → localhost:8000）。前端默认 X-User-Id=system 是 admin
 * 角色，/menu-config/tree 与 PUT 均可达。
 */

import { expect, test, type Page } from "@playwright/test";

const BASE = "http://localhost:5173";
const ADMIN_MENUS = "/admin/menus";

/** 用 page.request 走 Playwright 自己的 baseURL（不依赖 page.evaluate 的 fetch）。 */
async function apiGet(page: Page, path: string): Promise<unknown> {
  const r = await page.request.get(`${BASE}/api/v1${path}`, {
    headers: { "X-User-Id": "system" },
  });
  expect(r.status()).toBe(200);
  const env = await r.json();
  return (env as { data?: unknown }).data ?? env;
}

async function apiPut(
  page: Page,
  path: string,
  body: Record<string, unknown>,
): Promise<number> {
  const r = await page.request.put(`${BASE}/api/v1${path}`, {
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": "system",
    },
    data: body,
  });
  return r.status();
}

test.describe("菜单管理（feat-menu-tree）", () => {
  test("默认进入树形视图且渲染一级类", async ({ page }) => {
    await page.goto(ADMIN_MENUS);

    // 树形视图 Tab 处于 active 态（antd 给激活 tab 加 .ant-tabs-tab-active）
    const treeTab = page.locator(".ant-tabs-tab", { hasText: "树形视图" }).first();
    await expect(treeTab).toBeVisible();
    await expect(treeTab).toHaveClass(/ant-tabs-tab-active/);

    // antd Tree 渲染：至少 1 个 section 节点（labelKey 形式）
    await expect(page.getByText(/menu\.section\.aiAgent/)).toBeVisible();
    // 列表视图能见到 item.chat → 证明 /tree 也确实返回了叶子
    await page.locator(".ant-tabs-tab", { hasText: "列表视图" }).first().click();
    await expect(page.getByText("item.chat").first()).toBeVisible();
  });

  test("切换到列表视图后 Table 渲染", async ({ page }) => {
    await page.goto(ADMIN_MENUS);

    await page.locator(".ant-tabs-tab", { hasText: "列表视图" }).first().click();

    // Table 列头：代码 / 排序 / 可见 等（限定在 .ant-table 内，避免误中 sidebar）
    const table = page.locator(".ant-table");
    await expect(table).toBeVisible();
    await expect(table.getByText(/^代码$/)).toBeVisible();
    await expect(table.getByText(/^排序$/)).toBeVisible();
    await expect(table.getByText(/^可见$/)).toBeVisible();

    // AI Agent 一级类出现在 Table 里
    await expect(table.getByText("section.aiAgent").first()).toBeVisible();
  });

  test("PUT /menu-config/{code} 改 sortOrder 后列表视图看到新值", async ({ page }) => {
    // 1. 读现值
    const list = (await apiGet(page, "/menu-config/admin")) as Array<{
      code: string;
      sortOrder: number;
    }>;
    const target = list.find((x) => x.code === "item.agents");
    expect(target).toBeTruthy();
    const origSort = target!.sortOrder;
    const newSort = origSort === 999 ? 1000 : 999;

    // 2. PUT 改 sortOrder
    expect(await apiPut(page, "/menu-config/item.agents", { sortOrder: newSort })).toBe(200);

    // 3. 列表视图可见新 sortOrder
    await page.goto(ADMIN_MENUS);
    await page.locator(".ant-tabs-tab", { hasText: "列表视图" }).first().click();
    await expect(page.getByText(newSort.toString()).first()).toBeVisible();

    // 4. 还原
    expect(
      await apiPut(page, "/menu-config/item.agents", { sortOrder: origSort }),
    ).toBe(200);
  });

  test("PUT /menu-config/{code} 改 parentCode 后树形视图归属更新", async ({ page }) => {
    const list = (await apiGet(page, "/menu-config/admin")) as Array<{
      code: string;
      parentCode: string | null;
    }>;
    const target = list.find((x) => x.code === "item.agents");
    expect(target).toBeTruthy();
    const beforeParent = target!.parentCode;
    expect(beforeParent).toBeTruthy();

    // 1. PUT 切到 section.systemConfig
    expect(
      await apiPut(page, "/menu-config/item.agents", {
        parentCode: "section.systemConfig",
      }),
    ).toBe(200);

    // 2. 重新进入页面（树形视图默认）→ 验证 systemConfig 一级类存在
    await page.goto(ADMIN_MENUS);
    const tree = page.locator(".ant-tree");
    await expect(tree.getByText(/menu\.section\.systemConfig/)).toBeVisible();
    // 切到列表视图确认 item.agents 归属已变（labelKey→i18n: 系统信息配置）
    await page.locator(".ant-tabs-tab", { hasText: "列表视图" }).first().click();
    const table = page.locator(".ant-table");
    const row = table.locator("tr", { hasText: "item.agents" }).first();
    await expect(row).toContainText(/系统信息配置/);

    // 3. 还原
    expect(
      await apiPut(page, "/menu-config/item.agents", { parentCode: beforeParent }),
    ).toBe(200);
  });

  test("叶子项拖到非 section（叶子）→ 后端 422 拒绝 + parent 未变", async ({ page }) => {
    const listBefore = (await apiGet(page, "/menu-config/admin")) as Array<{
      code: string;
      parentCode: string | null;
    }>;
    const before = listBefore.find((x) => x.code === "item.chat")!.parentCode;
    expect(before).toBe("section.aiAgent");

    // 把 item.chat.parentCode 改成 item.agentRuntime（叶子）→ 422
    expect(
      await apiPut(page, "/menu-config/item.chat", {
        parentCode: "item.agentRuntime",
      }),
    ).toBe(422);

    // 确认 parent 未变
    const listAfter = (await apiGet(page, "/menu-config/admin")) as Array<{
      code: string;
      parentCode: string | null;
    }>;
    const after = listAfter.find((x) => x.code === "item.chat")!.parentCode;
    expect(after).toBe("section.aiAgent");
  });
});