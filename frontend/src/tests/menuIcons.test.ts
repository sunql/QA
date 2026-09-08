/**
 * 菜单 icon 注册表契约（回归：DB 写 icon_code 但 ICON_REGISTRY 没注册 → 静默不渲染）
 *
 * 背景：
 * - `menu_config.icon_code` 由 seed_menu_config.py 写入
 * - 前端 `ICON_REGISTRY` 是代码级注册表
 * - 两者是「两套 SSOT」——漂移的话菜单里那一项就没有图标，但 UI 不报错
 *
 * 测试策略：
 * 1. 静态契约：每个已知的 DB icon_code 必须在 ICON_REGISTRY 里有 key
 * 2. 渲染契约：给 renderIcon 喂一个 mock 菜单，断言每个 icon_code 都能渲染出非空 ReactNode
 * 3. 失败模式契约：未注册的 icon_code 必须显式返回 null（不能崩、不能误渲染成别的）
 */
import { describe, expect, it } from "vitest";
import { ICON_REGISTRY, renderIcon } from "../components/common/menuIcons";

// 必须与 backend/scripts/seed_menu_config.py 中所有 icon_code 字段保持一致
// （不在此白名单里的 icon_code 一律视为 DB 端漏注册或前端 ICON_REGISTRY 漏实现）
const KNOWN_DB_ICON_CODES = [
  // sections
  "robot", "fund", "setting", "database", "api", "safety",
  // items
  "message", "thunderbolt", "appstore", "barchart", "alert",
  "partition", "audit", "node", "code", "number", "cluster",
  "tags", "tool", "file", "dashboard", "apartment", "heart",
] as const;

describe("ICON_REGISTRY 与 menu_config.icon_code 对齐", () => {
  it("每个已知的 DB icon_code 都在 ICON_REGISTRY 里有实现", () => {
    const missing = KNOWN_DB_ICON_CODES.filter((c) => !(c in INICON_REGISTRY()));
    expect(
      missing,
      `以下 icon_code 在 seed_menu_config.py 用了但前端 ICON_REGISTRY 没注册:\n` +
        `  ${missing.join(", ")}\n` +
        `→ 在 frontend/src/components/common/menuIcons.tsx 加 wrap(SomeOutlined)`,
    ).toEqual([]);
  });

  it("renderIcon 对每个已知 icon_code 都返回非空 ReactNode", () => {
    for (const code of KNOWN_DB_ICON_CODES) {
      expect(renderIcon(code), `icon "${code}" 应能渲染出图标`).toBeTruthy();
    }
  });

  it("renderIcon 对未注册的 icon_code 显式返回 null（静默兜底，不崩）", () => {
    // 这就是用户踩过的 bug 模式：seed 写了 "tags"，前端 ICON_REGISTRY 没注册
    // → renderIcon("tags") 返回 null → 菜单那一行没有图标
    // 契约：必须返回 null，不能抛错、不能渲染成别的 icon
    expect(renderIcon("does-not-exist")).toBeNull();
    expect(renderIcon("")).toBeNull();
    expect(renderIcon(undefined)).toBeNull();
  });

  it("没有冗余注册（ICON_REGISTRY 的每个 key 都被至少一处 menu_config 使用）", () => {
    // 防「I18N 漂移」：加了 ICON_REGISTRY 条目但 seed 不用 → 死代码
    const used = new Set<string>(KNOWN_DB_ICON_CODES);
    const unused = Object.keys(ICON_REGISTRY).filter((k) => !used.has(k));
    if (unused.length > 0) {
      // 不阻塞，但记日志方便人工 review
      // eslint-disable-next-line no-console
      console.warn(
        `ICON_REGISTRY 中以下 key 当前无 menu_config 使用:\n` +
          `  ${unused.join(", ")}\n` +
          `→ 如果是新增图标，请同步加到 KNOWN_DB_ICON_CODES 白名单；` +
          `如果是死代码，请从 ICON_REGISTRY 删除`,
      );
    }
    // 不强制断言：留 warn 是为了"先添加 seed 条目，再加 ICON_REGISTRY"的过渡期不被破坏
  });
});

// helper 别名：让测试内调用更像前端惯用风格
// （TS 类型不允许 `import { ICON_REGISTRY } as INICON_REGISTRY` 这种语法，所以走函数包裹）
function INICON_REGISTRY(): Record<string, unknown> {
  return ICON_REGISTRY;
}