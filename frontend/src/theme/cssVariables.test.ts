/** cssVariables 纯函数单元测试（Phase A RED）。
 *
 * 覆盖：
 * - tokensToCssVars：把 token 对象转换为 :root 选择器下的 CSS 变量声明
 * - applyCssVarsToRoot：把生成的 CSS 字符串写入 document.documentElement.style
 * - removeCssVarsFromRoot：清除所有相关 CSS 变量
 */
import { describe, it, expect, beforeEach } from "vitest";
import {
  tokensToCssVars,
  applyCssVarsToRoot,
  removeCssVarsFromRoot,
} from "./cssVariables";
import type { ThemeToken } from "./tokens";

const TEST_TOKEN: ThemeToken = {
  colorPrimary: "#00D9C0",
  colorBgBase: "#0f1e2e",
  colorBgContainer: "#152838",
  colorBorder: "#1f3a52",
  colorSuccess: "#00D9C0",
  colorWarning: "#FF8C42",
  colorError: "#F56C6C",
  colorText: "#FFFFFF",
  colorTextSecondary: "#cbd5e1",
  colorTextTertiary: "#94a3b8",
  borderRadius: 2,
  borderRadiusLG: 4,
  layers: {
    layerSourceSystem: "#1677ff",
    layerOds: "#13c2c2",
    layerDwd: "#52c41a",
    layerDws: "#722ed1",
    layerAds: "#fa8c16",
    layerKpi: "#f5222d",
    layerAi: "#eb2f96",
  },
};

describe("tokensToCssVars", () => {
  it("生成包含所有 token 的 CSS 变量声明", () => {
    const css = tokensToCssVars(TEST_TOKEN);
    expect(css).toContain("--color-primary: #00D9C0");
    expect(css).toContain("--color-bg-base: #0f1e2e");
    expect(css).toContain("--color-bg-container: #152838");
    expect(css).toContain("--color-border: #1f3a52");
    expect(css).toContain("--color-success: #00D9C0");
    expect(css).toContain("--color-warning: #FF8C42");
    expect(css).toContain("--color-error: #F56C6C");
    expect(css).toContain("--color-text: #FFFFFF");
    expect(css).toContain("--color-text-secondary: #cbd5e1");
    expect(css).toContain("--color-text-tertiary: #94a3b8");
    expect(css).toContain("--border-radius: 2px");
    expect(css).toContain("--border-radius-lg: 4px");
    // Phase D：血缘层级色板
    expect(css).toContain("--color-layer-source-system: #1677ff");
    expect(css).toContain("--color-layer-ods: #13c2c2");
    expect(css).toContain("--color-layer-dwd: #52c41a");
    expect(css).toContain("--color-layer-dws: #722ed1");
    expect(css).toContain("--color-layer-ads: #fa8c16");
    expect(css).toContain("--color-layer-kpi: #f5222d");
    expect(css).toContain("--color-layer-ai: #eb2f96");
  });

  it("生成的 CSS 字符串包裹在 :root 选择器内", () => {
    const css = tokensToCssVars(TEST_TOKEN);
    expect(css).toContain(":root");
    expect(css.trim().startsWith(":root")).toBe(true);
    expect(css.trim().endsWith("}")).toBe(true);
  });

  it("返回纯函数结果（同输入同输出，不修改入参）", () => {
    const a = tokensToCssVars(TEST_TOKEN);
    const b = tokensToCssVars(TEST_TOKEN);
    expect(a).toBe(b); // 同样输入产出同样字符串引用（设计选择：稳定缓存友好）
  });
});

describe("applyCssVarsToRoot", () => {
  beforeEach(() => {
    document.documentElement.style.cssText = "";
  });

  it("把生成的 CSS 写入 document.documentElement.style", () => {
    applyCssVarsToRoot(TEST_TOKEN);
    const inline = document.documentElement.style.cssText;
    expect(inline).toContain("--color-primary");
    expect(inline).toContain("#00D9C0");
    expect(inline).toContain("--color-bg-base");
  });

  it("二次调用会覆盖前一次的 CSS 变量（不留残留）", () => {
    applyCssVarsToRoot(TEST_TOKEN);
    applyCssVarsToRoot({ ...TEST_TOKEN, colorPrimary: "#000000" });
    const inline = document.documentElement.style.cssText;
    // 应只剩最新的主色；--color-primary 应为新值
    // 注意：--color-success 仍是 #00D9C0（DARK_TOKEN 设计如此），所以不能用全局 not.toContain 断言
    expect(inline).toContain("--color-primary: #000000");
    // 旧主色不再出现在 --color-primary 变量声明中（通过正则匹配该变量名）
    expect(inline).not.toMatch(/--color-primary:\s*#00D9C0/);
  });
});

describe("removeCssVarsFromRoot", () => {
  beforeEach(() => {
    document.documentElement.style.cssText = "";
  });

  it("清除所有由 applyCssVarsToRoot 写入的 CSS 变量", () => {
    applyCssVarsToRoot(TEST_TOKEN);
    expect(document.documentElement.style.cssText).toContain("--color-primary");
    removeCssVarsFromRoot();
    expect(document.documentElement.style.cssText).not.toContain("--color-primary");
    expect(document.documentElement.style.cssText).not.toContain("--color-bg-base");
  });

  it("对空 style 调用也不抛错（幂等）", () => {
    expect(() => removeCssVarsFromRoot()).not.toThrow();
  });
});