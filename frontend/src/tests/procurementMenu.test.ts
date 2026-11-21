/**
 * M4 菜单行契约守卫：seed_menu_config.py ↔ i18n ↔ 前端路由三方对齐。
 *
 * 背景：菜单是 DB 行（seed 提供初始值），路径写错 ⇒ 侧边栏点了 404；
 * label_key 缺 i18n 键 ⇒ 菜单显示原始 key。两者都不报错 ⇒ 只能靠守卫钉住。
 *
 * 三条判据：
 * 1. **路径可达**：M4 四个页面的 path 必须在 App.tsx 有对应 Route；
 * 2. **i18n 齐全**：每个 label_key 在 zh-CN 与 en-US 都有值（非原始 key）；
 * 3. **icon 可渲染**：icon_code 在 ICON_REGISTRY 有注册。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { ICON_REGISTRY } from "../components/common/menuIcons";
import { zhCN } from "../i18n/zh-CN";
import { enUS } from "../i18n/en-US";

const REPO_ROOT = resolve(__dirname, "..", "..", "..");

function readSeed(): { sections: string[]; items: string[] } {
  const src = readFileSync(
    resolve(REPO_ROOT, "backend/scripts/seed_menu_config.py"),
    "utf8",
  );
  const sections = [...src.matchAll(/"code":\s*"(section\.[A-Za-z]+)"/g)].map(
    (m) => m[1],
  );
  const items = [...src.matchAll(/"code":\s*"(item\.[A-Za-z]+)"/g)].map(
    (m) => m[1],
  );
  return { sections, items };
}

const PROCUREMENT_PATHS = [
  "/procurement/rfq",
  "/procurement/bindings",
  "/procurement/knowledge",
  "/procurement/agents",
] as const;

describe("M4 采购协同菜单契约", () => {
  it("seed 里有 section.procurement 与 4 个采购叶子项", () => {
    const { sections, items } = readSeed();
    expect(sections).toContain("section.procurement");
    expect(items).toEqual(
      expect.arrayContaining([
        "item.procurementRfq",
        "item.procurementBindings",
        "item.procurementKnowledge",
        "item.procurementAgents",
      ]),
    );
  });

  it("每个采购叶子项的 path 在 App.tsx 有对应 Route（否则点了 404）", () => {
    const appSrc = readFileSync(resolve(REPO_ROOT, "frontend/src/App.tsx"), "utf8");
    for (const path of PROCUREMENT_PATHS) {
      // App.tsx 用**相对**路径（无前导 /，挂在 AppLayout 之下）
      const relative = path.replace(/^\//, "");
      expect(appSrc, `App.tsx 缺路由 ${path}`).toContain(
        `path="${relative}"`,
      );
    }
  });

  it("采购菜单的 label_key 在 zh-CN 与 en-US 都有译文", () => {
    const labelKeys = [
      "menu.section.procurement",
      "menu.item.procurementRfq",
      "menu.item.procurementBindings",
      "menu.item.procurementKnowledge",
      "menu.item.procurementAgents",
    ];
    const zh = zhCN as unknown as Record<string, unknown>;
    const en = enUS as unknown as Record<string, unknown>;
    for (const key of labelKeys) {
      const zhValue = key
        .split(".")
        .reduce<unknown>((acc, part) => (acc as Record<string, unknown>)?.[part], zh);
      const enValue = key
        .split(".")
        .reduce<unknown>((acc, part) => (acc as Record<string, unknown>)?.[part], en);
      expect(typeof zhValue, `zh-CN 缺 ${key}`).toBe("string");
      expect(typeof enValue, `en-US 缺 ${key}`).toBe("string");
      expect(zhValue).not.toBe(key);
    }
  });

  it("采购菜单用的 icon_code 都已在 ICON_REGISTRY 注册", () => {
    for (const code of ["workflow", "partition", "book", "appstore"]) {
      expect(code in ICON_REGISTRY, `ICON_REGISTRY 缺 ${code}`).toBe(true);
    }
  });
});
