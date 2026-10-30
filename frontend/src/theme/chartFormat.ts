/** 图表数字格式化（主题表现层）。
 *
 * **为什么在前端**：后端只发 JSON（SSE 契约），ECharts 的 formatter 是 JS 函数，
 * 没法跨 JSON 下发 —— 所以轴缩写 / 千分位只能在前端注入。注入点是主题层：
 * 与 `applyChartTheme` 同样的「只补缺省，不动已有」纪律，且绝不遍历 series
 * （饼图 `{b}: {d}%`、瀑布图 `{c}` 这类结构模板保持逐字）。
 *
 * 口径（zh-CN）：
 * - 轴（`formatAxisValue`）：≥1e8 → 亿、≥1e4 → 万（刻度要短，挤了会糊）；
 * - tooltip（`formatTooltipValue`）：永远千分位 + ≤2 位小数（悬停要读准数）；
 * - 其他 locale 不发明 K/M，只走千分位。
 */
type ChartOption = Record<string, unknown>;

const YI = 100_000_000;
const WAN = 10_000;

function isPlainObject(value: unknown): value is ChartOption {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))) {
    return Number(value);
  }
  return null;
}

/** 分组数字（千分位 + 最多 2 位小数）。 */
function grouped(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value);
}

/** 数值轴刻度：zh 语境万/亿缩写，其他 locale 千分位；非有限数 → ""（跳过刻度）。 */
export function formatAxisValue(value: unknown, locale = "zh-CN"): string {
  const num = toFiniteNumber(value);
  if (num === null) return "";
  if (locale !== "zh-CN") return grouped(num, locale);
  const abs = Math.abs(num);
  if (abs >= YI) return `${grouped(num / YI, locale)}亿`;
  if (abs >= WAN) return `${grouped(num / WAN, locale)}万`;
  return grouped(num, locale);
}

/** tooltip 数值：数组递归（双轴多值）；null/undefined → ""；其余字符串化。 */
export function formatTooltipValue(value: unknown, locale = "zh-CN"): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map((one) => formatTooltipValue(one, locale)).join(", ");
  const num = toFiniteNumber(value);
  if (num !== null) return grouped(num, locale);
  return String(value);
}

/** 给轴对象补 formatter：已有 formatter（函数/字符串）一律不动。 */
function withNumberFormat(axis: ChartOption, locale: string): ChartOption {
  if (axis.type !== "value" && axis.type !== "log") return axis;
  const axisLabel = isPlainObject(axis.axisLabel) ? axis.axisLabel : {};
  if (axisLabel.formatter !== undefined) return axis;
  return { ...axis, axisLabel: { ...axisLabel, formatter: (value: unknown) => formatAxisValue(value, locale) } };
}

/** 轴可以是单对象或对象数组（双轴组合图），两种都覆盖；非轴对象原样返回。 */
function withNumberFormatAll(axis: unknown, locale: string): unknown {
  if (Array.isArray(axis)) return axis.map((one) => (isPlainObject(one) ? withNumberFormat(one, locale) : one));
  return isPlainObject(axis) ? withNumberFormat(axis, locale) : axis;
}

/** 把数字 formatter 注入 option（不可变；非对象原样返回 —— 后端 JSON 形状不可信）。
 *
 * 只碰 `xAxis` / `yAxis` 的 value/log 轴与 tooltip 的 valueFormatter；tooltip
 * 已有 valueFormatter 时不覆盖。series 不被遍历 —— 结构模板（`{b}: {d}%`、`{c}`）
 * 属于后端，这里逐字保留。
 */
export function applyChartNumberFormat(
  option: Record<string, unknown>,
  locale = "zh-CN"
): Record<string, unknown> {
  if (!isPlainObject(option)) return option;

  const tooltip = isPlainObject(option.tooltip) ? option.tooltip : undefined;
  const patchedTooltip =
    tooltip && tooltip.valueFormatter === undefined
      ? {
          ...tooltip,
          valueFormatter: (value: unknown) => formatTooltipValue(value, locale),
        }
      : tooltip;

  return {
    ...option,
    ...(option.xAxis !== undefined ? { xAxis: withNumberFormatAll(option.xAxis, locale) } : {}),
    ...(option.yAxis !== undefined ? { yAxis: withNumberFormatAll(option.yAxis, locale) } : {}),
    ...(patchedTooltip ? { tooltip: patchedTooltip } : {}),
  };
}
