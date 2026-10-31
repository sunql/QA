/**
 * 图表高度（2026-10-31）：类别一多，整张图连同坐标轴被等比压扁。
 *
 * 高度只从 **option 结构**推断，不依赖后端新字段 —— 在线渲染、PNG 导出、
 * 历史回放拿到的都是同一份服务端 option，因此三处天然一致。
 * - 类目轴挂在 yAxis = 横向条形图（决策引擎 >15 行时的形态）
 * - heatmap 按行数增长，单行更矮
 * - 其余 320 基线（纵向柱/折线/饼的类别数由宽度吸收）
 */
const CHART_BASE_HEIGHT = 320;
const CHART_MAX_HEIGHT = 960;
const HBAR_PADDING = 96;
const HBAR_ROW_HEIGHT = 28;
const CHART_LEGEND_HEIGHT = 32;
const HEATMAP_PADDING = 120;
const HEATMAP_ROW_HEIGHT = 26;

type OptionRecord = Record<string, unknown>;

function asRecord(value: unknown): OptionRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as OptionRecord)
    : null;
}

function categoryCountOf(axis: unknown): number {
  const record = asRecord(axis);
  if (!record || record.type !== "category" || !Array.isArray(record.data)) {
    return 0;
  }
  return record.data.length;
}

function firstSeriesType(option: OptionRecord): string | null {
  const first = Array.isArray(option.series) ? option.series[0] : option.series;
  const record = asRecord(first);
  return typeof record?.type === "string" ? record.type : null;
}

function hasLegend(option: OptionRecord): boolean {
  const legend = option.legend;
  if (Array.isArray(legend)) return legend.length > 0;
  const record = asRecord(legend);
  return record !== null && record.show !== false;
}

function clamp(height: number): number {
  return Math.min(CHART_MAX_HEIGHT, Math.max(CHART_BASE_HEIGHT, Math.round(height)));
}

export function chartHeightOf(
  option: OptionRecord | null | undefined
): number {
  const record = asRecord(option);
  if (!record) return CHART_BASE_HEIGHT;

  const rowCount = categoryCountOf(record.yAxis);
  if (rowCount === 0) return CHART_BASE_HEIGHT;

  if (firstSeriesType(record) === "heatmap") {
    return clamp(HEATMAP_PADDING + rowCount * HEATMAP_ROW_HEIGHT);
  }
  const legendExtra = hasLegend(record) ? CHART_LEGEND_HEIGHT : 0;
  return clamp(HBAR_PADDING + rowCount * HBAR_ROW_HEIGHT + legendExtra);
}
