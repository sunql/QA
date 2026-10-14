/** 可视化输出策略（Task 8）：把后端 rationale 转成前端可渲染的本地化文案（纯函数）。
 *
 * 职责单一 —— 只算「取哪个 i18n key、传什么插值 vars」：
 * 1. 拼 `chat.visual.<code>`（code 是后端 ruleId，前端**不内置白名单**）；
 * 2. `params.kind` 先经 `chatPanel.chartTypes.<kind>` 本地化成图型名再插值 —— 直接
 *    插枚举值会渲染成「数据结构不满足 bar 的绘图要求」；
 * 3. 插值 vars 只保留 string/number/boolean（`params` 是 `unknown` 值集，安全取值）；
 * 4. i18n 缺 key 时回退显示 code 原文（不炸）。
 *
 * `t` 由调用方注入（useTranslation 的 `t`），保持本模块零 React 依赖、可单测。
 */
import type { VisualRationale } from "../types/chat";
import type { Vars } from "../i18n/types";

type TFunction = (key: string, vars?: Vars) => string;

export function visualRationaleText(
  rationale: VisualRationale | null | undefined,
  t: TFunction
): string | null {
  if (!rationale) return null;

  const vars: Record<string, string | number | boolean> = {};
  for (const [key, value] of Object.entries(rationale.params)) {
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      vars[key] = value;
    }
  }

  const rawKind = rationale.params.kind;
  if (typeof rawKind === "string" && rawKind.length > 0) {
    const chartTypeKey = `chatPanel.chartTypes.${rawKind}`;
    const localized = t(chartTypeKey);
    // 缺 key 时 t 返回 key 自身 —— 那就不该拿 key 当图型名，回退枚举原文
    vars.kind = localized === chartTypeKey ? rawKind : localized;
  }

  const key = `chat.visual.${rationale.code}`;
  const text = t(key, vars);
  return text === key ? rationale.code : text;
}
