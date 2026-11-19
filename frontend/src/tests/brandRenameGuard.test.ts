// 守卫：系统名称改名必须**四处同时**生效（AI服务平台 → 泰鸿智脑）。
//
// 背景（2026-11-19，feat/brand-rename）：产品名从「AI服务平台」改为「泰鸿智脑」，
// 纯前端、零 DDL、零后端改动。名称散落在五个互不相干的地方：
//   ① 浏览器标签页标题 —— 烤在 `index.html` 的 `<title>`；
//   ② 浏览器 favicon —— `index.html` 里内联的 data-URI SVG（字母 Q → T）；
//   ③ 侧边栏顶部品牌 —— i18n 字典 `appLayout.brand`（zh-CN 与 en-US 两份）；
//   ④ 侧边栏折叠态缩写 —— `AppLayout.tsx` 里硬编码的 "QA" → "TH"；
//   ⑤ 登录页标题 —— i18n 字典 `auth.login.title`（顺带去掉内部代号 `qa-system`）。
//
// 为什么要有这条守卫：改名的失败模式不是「改错」，而是**只改了几处** ——
// 例如改了 i18n 忘了 `<title>`，或改了 zh-CN 忘了 en-US。
// 五处里任何一处漏改，用户都会在某个界面看到旧名（本仓已因分页/守卫等批次
// 反复出现「同一语义散落在多个文件、漏一处就自相矛盾」的形态）。
//
// 本守卫只做**正向断言**：逐个点名 5 个位置 + 新名字。
// 不用「反向扫描旧名字」的形式，因为反向扫描会把**注释里**的说明文字也算进去
// （本仓已有同类教训，见 src/tests/authTokenCleanupGuard.test.ts 的文件头注释），
// 且任何一处漏改都会被上面的正向断言直接捕获 —— 反向断言不提供额外覆盖。
//
// 断言用行首锚定的正则（`^\s*key:\s*"值",$`）而不是 AST：
//   - `// brand: "x"` 这类注释行以 `//` 或 `*` 开头 ⇒ `^\s*` 之后不是 `brand` ⇒ 匹配不到；
//   - 块注释同理（`* brand: ...`）⇒ 注释免疫。

import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";

/** 本文件位于 src/tests/ ⇒ 上一级是 src/，上上级是 frontend/。用 import.meta.dirname 定位（不依赖 cwd）。 */
const HERE = (import.meta as unknown as { dirname?: string }).dirname ?? process.cwd();
const SRC_DIR = existsSync(join(HERE, "..", "i18n", "zh-CN.ts"))
  ? resolve(HERE, "..")
  : resolve(process.cwd(), "src");
const FRONTEND_DIR = resolve(SRC_DIR, "..");

/** 读文件内容；路径相对 frontend/ 根目录。 */
const read = (...segments: string[]): string =>
  readFileSync(resolve(FRONTEND_DIR, ...segments), "utf-8");

describe("品牌改名守卫（AI服务平台 → 泰鸿智脑）", () => {
  it("① 浏览器标签页标题是「泰鸿智脑」", () => {
    expect(read("index.html")).toMatch(/<title>\s*泰鸿智脑\s*<\/title>/);
  });

  it("② favicon 的字母是 T 而不是 Q", () => {
    const html = read("index.html");
    expect(html).toContain("%3ET%3C/text%3E");
    expect(html).not.toContain("%3EQ%3C/text%3E");
  });

  it("③ 侧边栏品牌：中文「泰鸿智脑」/ 英文「Taihong ZhiNao」", () => {
    expect(read("src", "i18n", "zh-CN.ts")).toMatch(/^\s*brand:\s*"泰鸿智脑",$/m);
    expect(read("src", "i18n", "en-US.ts")).toMatch(/^\s*brand:\s*"Taihong ZhiNao",$/m);
  });

  it("④ 侧边栏折叠态缩写是 TH", () => {
    expect(read("src", "components", "common", "AppLayout.tsx")).toMatch(/collapsed \? "TH" :/);
  });

  it("登录页标题不再暴露内部代号 qa-system", () => {
    expect(read("src", "i18n", "zh-CN.ts")).toMatch(/^\s*title:\s*"登录 · 泰鸿智脑",$/m);
    expect(read("src", "i18n", "en-US.ts")).toMatch(/^\s*title:\s*"Sign in to Taihong ZhiNao",$/m);
  });
});
