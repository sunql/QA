/** 把 ThemeToken 序列化为 :root CSS 变量声明，并应用到 document.documentElement。
 *
 * 设计要点：
 * - tokensToCssVars 是纯函数：同输入同输出，引用稳定（缓存友好）。
 * - applyCssVarsToRoot 使用 cssText 整体替换，切换主题时不会留下旧变量。
 * - removeCssVarsFromRoot 幂等：清空时不抛错。
 *
 * 与 antd theme.token 的关系：
 * - antd 内部消费 token 走 React 上下文 + CSS-in-JS；
 * - 本模块产出的 CSS 变量供 ECharts / inline style / 业务自定义组件消费。
 * - 两者使用相同的色源（见 tokens.ts），保持视觉一致。
 */
import type { ThemeToken } from "./tokens";

/** 把 ThemeToken 序列化为 :root { ... } 形式的 CSS 字符串。 */
export function tokensToCssVars(token: ThemeToken): string {
  // CSS 变量声明顺序：颜色 → 圆角 → 状态色 → 血缘层色。空格统一用单空格，便于人类阅读。
  const declarations = [
    `--color-primary: ${token.colorPrimary}`,
    `--color-bg-base: ${token.colorBgBase}`,
    `--color-bg-container: ${token.colorBgContainer}`,
    `--color-border: ${token.colorBorder}`,
    `--color-success: ${token.colorSuccess}`,
    `--color-warning: ${token.colorWarning}`,
    `--color-error: ${token.colorError}`,
    `--color-text: ${token.colorText}`,
    `--color-text-secondary: ${token.colorTextSecondary}`,
    `--color-text-tertiary: ${token.colorTextTertiary}`,
    `--border-radius: ${token.borderRadius}px`,
    `--border-radius-lg: ${token.borderRadiusLG}px`,
    // 血缘层级色板（Phase D：ECharts token 化）
    `--color-layer-source-system: ${token.layers.layerSourceSystem}`,
    `--color-layer-ods: ${token.layers.layerOds}`,
    `--color-layer-dwd: ${token.layers.layerDwd}`,
    `--color-layer-dws: ${token.layers.layerDws}`,
    `--color-layer-ads: ${token.layers.layerAds}`,
    `--color-layer-kpi: ${token.layers.layerKpi}`,
    `--color-layer-ai: ${token.layers.layerAi}`,
  ];
  return `:root { ${declarations.join("; ")}; }`;
}

/** 把 token 生成的 CSS 变量整体写入 document.documentElement.style。
 *
 * 注意：使用 cssText 整体覆盖，切换主题时旧变量自动消失，无需手动逐项 removeProperty。
 */
export function applyCssVarsToRoot(token: ThemeToken): void {
  if (typeof document === "undefined") return; // SSR 安全
  const css = tokensToCssVars(token);
  // 从 ":root { ... }" 中提取声明体，写入 style.cssText
  const body = css.replace(/^:root\s*\{\s*/, "").replace(/\s*\}\s*$/, "");
  document.documentElement.style.cssText = body;
}

/** 清除所有由 applyCssVarsToRoot 写入的 CSS 变量。幂等。 */
export function removeCssVarsFromRoot(): void {
  if (typeof document === "undefined") return;
  // 通过解析当前 cssText 移除已知变量名（避免误删其他模块设置的属性）
  const style = document.documentElement.style;
  const knownProps = [
    "--color-primary",
    "--color-bg-base",
    "--color-bg-container",
    "--color-border",
    "--color-success",
    "--color-warning",
    "--color-error",
    "--color-text",
    "--color-text-secondary",
    "--color-text-tertiary",
    "--border-radius",
    "--border-radius-lg",
    "--color-layer-source-system",
    "--color-layer-ods",
    "--color-layer-dwd",
    "--color-layer-dws",
    "--color-layer-ads",
    "--color-layer-kpi",
    "--color-layer-ai",
  ];
  for (const prop of knownProps) {
    style.removeProperty(prop);
  }
}