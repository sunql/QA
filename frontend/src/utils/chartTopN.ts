/**
 * Top-N 截断视图的**唯一选择口径**（2026-10-31）。
 *
 * 后端渲染时若判定是排名语义且类目 > 20，会在同一份 option 上附加：
 * - `topNOption`：前 20 类 + 「其他（N 项）」的完整 ECharts option；
 * - `topNMeta`：`{limit, hidden, total}`，供切换按钮写文案、也用于**成对校验**。
 *
 * 前端不自造截断规则（那条线在后端 `chart_renderer`），只决定「默认看哪一份」：
 * 在线渲染、PNG 截图、PDF 导出都从这里取，四处才不会各截各的。
 */

import { asChartOption } from "./chartContract";

export interface TopNMeta {
  limit: number;
  hidden: number;
  total: number;
}

function isPositiveInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

/**
 * 解析 `topNMeta`。任何一处不成立都判整份无效（返回 null）——
 * 半份元信息驱动出来的按钮与高度只会给出错位的提示，不如不给。
 */
export function topNMetaOf(
  option: Record<string, unknown> | null | undefined
): TopNMeta | null {
  const record = asChartOption(option);
  if (!record) return null;
  const meta = asChartOption(record.topNMeta);
  if (!meta) return null;
  const { limit, hidden, total } = meta;
  if (!isPositiveInt(limit) || !isPositiveInt(hidden) || !isPositiveInt(total)) {
    return null;
  }
  if (total <= limit || hidden !== total - limit) return null;
  return { limit, hidden, total };
}

/**
 * 截断视图。`topNOption` 与 `topNMeta` **必须成对**：只有视图没有元信息时
 * 切换按钮没有文案可用、高度也没法解释，按「没有截断」处理（老会话即此形态）。
 */
export function topNOptionOf(
  option: Record<string, unknown> | null | undefined
): Record<string, unknown> | null {
  if (!topNMetaOf(option)) return null;
  const record = asChartOption(option);
  return record ? asChartOption(record.topNOption) : null;
}

/** 默认展示的 option：有合格截断视图就截断，否则原样返回（同一引用，不复制）。 */
export function defaultChartOption(
  option: Record<string, unknown> | null | undefined
): Record<string, unknown> {
  const record = asChartOption(option);
  if (!record) return {};
  return topNOptionOf(record) ?? record;
}
