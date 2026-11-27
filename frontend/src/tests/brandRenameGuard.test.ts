// 守卫：品牌名**不再写死**，必须来自运行时配置（system_config）。
//
// 背景（2026-11-27，feat/configurable-brand）：产品名原先散落在 5 个互不相干的地方
//   ① 浏览器标签页标题 —— 烤在 `index.html` 的 `<title>`；
//   ② 侧边栏品牌 —— i18n 字典 `appLayout.brand`（zh-CN 与 en-US 两份）；
//   ③ 侧边栏折叠态缩写 —— `AppLayout.tsx` 里硬编码的 "TH"；
//   ④ 登录页标题 —— i18n 字典 `auth.login.title`。
// 改名要改 4 处、漏一处就自相矛盾（改 i18n 忘了 <title>，或改了 zh 忘了 en）。
//
// 现在品牌名是**运行时参数**：
//   system_config 的 4 个键 → GET /api/v1/branding → brandingStore → UI
//
// 本守卫钉住「配置化」这个前提 —— 防止有人把品牌名又写回字面量：
//   ① `index.html` 的静态 `<title>` 仍是 **JS 加载前的兜底**，必须等于前端兜底常量；
//   ② `AppLayout.tsx` 不得出现品牌字面量，必须走 brandingStore；
//   ③ i18n 的登录页标题必须是 `{brand}` 插值，不得含字面品牌名；
//   ④ i18n 字典里不得再有 `appLayout.brand`（品牌名不是翻译项，是配置项）。
//
// 断言用源码文本而非运行时渲染：这里要守的是「**不许写死**」这条**约定**，
// 运行时渲染测不出来（运行时当然显示得出名字 —— 问题是名字从哪来）。
//
// 断言用行首锚定的正则（`^\s*key:\s*"值",$`）而不是 AST：
//   - `// brand: "x"` 这类注释行以 `//` 或 `*` 开头 ⇒ `^\s*` 之后不是 `brand` ⇒ 匹配不到；
//   - 块注释同理（`* brand: ...`）⇒ 注释免疫。

import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";

import {
  DEFAULT_BRAND_NAME,
  DEFAULT_BRAND_NAME_EN,
  DEFAULT_BRAND_SHORT_NAME,
  DEFAULT_BRAND_TITLE,
} from "../branding/defaults";

/** 本文件位于 src/tests/ ⇒ 上一级是 src/，上上级是 frontend/。用 import.meta.dirname 定位（不依赖 cwd）。 */
const HERE = (import.meta as unknown as { dirname?: string }).dirname ?? process.cwd();
const SRC_DIR = existsSync(join(HERE, "..", "i18n", "zh-CN.ts"))
  ? resolve(HERE, "..")
  : resolve(process.cwd(), "src");
const FRONTEND_DIR = resolve(SRC_DIR, "..");

/** 读文件内容；路径相对 frontend/ 根目录。 */
const read = (...segments: string[]): string =>
  readFileSync(resolve(FRONTEND_DIR, ...segments), "utf-8");

/** 去掉注释（块注释 + 行注释）——反向扫描前必须做，否则注释里的品牌名会造成假红。 */
const stripComments = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

/** 正则元字符转义（品牌名可能含括号 / 点号等）。 */
const escapeRegExp = (value: string): string =>
  value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

describe("品牌配置化守卫（品牌名必须是运行时参数，不是字面量）", () => {
  it("① index.html 的静态 <title> 等于前端兜底常量", () => {
    // 这是 JS 加载**前**的兜底（静态托管 ⇒ 首屏先看到它）。运行时会被
    // brandingStore 覆盖成 system_config 里的值。
    const html = read("index.html");
    const titlePattern = new RegExp(
      `<title>\\s*${escapeRegExp(DEFAULT_BRAND_TITLE)}\\s*</title>`,
    );
    expect(html).toMatch(titlePattern);
  });

  it("② 侧边栏品牌走 brandingStore，不写死字面量", () => {
    const appLayout = stripComments(read("src", "components", "common", "AppLayout.tsx"));
    // 展开态与折叠态都必须来自配置（曾经的 `collapsed ? "TH" : t("appLayout.brand")`）。
    expect(appLayout).toContain("useBrandingStore");
    expect(appLayout).toContain("branding.brandShortName");
    // 折叠态缩写不许是字面量（只查**字符串字面量**，避免命中注释里的 "TH"）
    expect(appLayout).not.toMatch(new RegExp(`["']${escapeRegExp(DEFAULT_BRAND_SHORT_NAME)}["']`));
    // 旧的折叠态字面量写法不能再出现
    expect(appLayout).not.toMatch(/collapsed\s*\?\s*"/);
    // **任何**中文字面量都不该出现在 AppLayout（它本来就没有中文文案 ⇒ 这条能抓住
    // 「换成另一个硬编码名字」的情况，而不只是抓住「又写回了泰鸿智脑」）。
    const chineseLiterals = appLayout.match(/["'][^"']*[一-鿿][^"']*["']/g) ?? [];
    expect(chineseLiterals).toEqual([]);
  });

  it("③ 登录页标题用 {brand} 插值，不含字面品牌名", () => {
    const zh = stripComments(read("src", "i18n", "zh-CN.ts"));
    const en = stripComments(read("src", "i18n", "en-US.ts"));
    expect(zh).toMatch(/^\s*title:\s*"登录 · \{brand\}",$/m);
    expect(en).toMatch(/^\s*title:\s*"Sign in to \{brand\}",$/m);
    expect(zh).not.toContain(DEFAULT_BRAND_NAME);
    expect(en).not.toContain(DEFAULT_BRAND_NAME_EN);
  });

  it("④ i18n 字典里不再有 appLayout.brand（品牌名不是翻译项）", () => {
    const zh = read("src", "i18n", "zh-CN.ts");
    const en = read("src", "i18n", "en-US.ts");
    expect(zh).not.toMatch(/^\s*brand:\s*"/m);
    expect(en).not.toMatch(/^\s*brand:\s*"/m);
  });

  it("⑤ favicon 仍是字母 T（静态资产，不参数化）", () => {
    // favicon 是内联 data-URI SVG，**不参与配置化**（见 plan §5「不做的事」）。
    // 换品牌名时若首字母变了，需要一并改这里 —— 守卫会提醒。
    const html = read("index.html");
    expect(html).toContain("%3ET%3C/text%3E");
    expect(html).not.toContain("%3EQ%3C/text%3E");
  });
});
