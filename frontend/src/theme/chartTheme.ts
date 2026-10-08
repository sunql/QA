/** 图表主题注入（图表决策引擎，决策 6）。
 *
 * 服务端**只发结构**：option 里没有 `color`、没有 `itemStyle.color`、没有轴与
 * 文字的颜色。颜色一律由这一层补 —— 于是「同一张图在亮/暗主题下都像样」这件事
 * 只在一个地方维护，而不是散在服务端的 renderer 里。
 *
 * 两条设计取舍：
 * - **只补色，不改结构**：series 的数据与类型一字不动。补错了顶多难看；动到结构
 *   就是画错了，而结构属于服务端。
 * - **不需要 `isDark` 参数**：轴色/文字色/tooltip 底色都从 token 取，而 token 本身
 *   就是按主题选的（`DARK_TOKEN` / `LIGHT_TOKEN`）。多传一个 isDark 等于给同一件
 *   事留第二个真相来源。
 */
import type { ThemeToken } from "./tokens";

/** ECharts option 的宽松形态：来自后端 JSON，形状不可信，只做逐键补色。 */
type ChartOption = Record<string, unknown>;

function isPlainObject(value: unknown): value is ChartOption {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 把 lineStyle 的 color 补上，保留已有键（虚线/宽度这类样式属于结构，不能覆盖）。 */
function withLineColor(container: unknown, token: ThemeToken): ChartOption {
  const base = isPlainObject(container) ? container : {};
  const lineStyle = isPlainObject(base.lineStyle) ? base.lineStyle : {};
  return { ...base, lineStyle: { ...lineStyle, color: token.colorBorder } };
}

/** 轴对象的形状补齐：轴色、刻度标签色、（仅数值轴）分割线色。 */
function withAxisTheme(axis: ChartOption, token: ThemeToken): ChartOption {
  const isValueAxis = axis.type === "value" || axis.type === "log";
  const patched: ChartOption = {
    ...axis,
    axisLine: withLineColor(axis.axisLine, token),
    axisLabel: { ...(isPlainObject(axis.axisLabel) ? axis.axisLabel : {}), color: token.colorTextTertiary },
  };
  if (isValueAxis) {
    patched.splitLine = withLineColor(axis.splitLine, token);
  }
  return patched;
}

/** 轴可以是单个对象或对象数组（双轴组合图），两种都要覆盖。 */
function withAxisThemeAll(axis: unknown, token: ThemeToken): unknown {
  if (Array.isArray(axis)) return axis.map((one) => (isPlainObject(one) ? withAxisTheme(one, token) : one));
  return isPlainObject(axis) ? withAxisTheme(axis, token) : axis;
}

/**
 * 把主题色注入 ECharts option，返回**新对象**（不可变）。
 *
 * 传入非对象（后端 JSON 形状不可信）时原样返回，不抛错 —— 图表渲染失败不该把
 * 整条消息带崩。
 */
export function applyChartTheme(
  option: Record<string, unknown>,
  token: ThemeToken
): Record<string, unknown> {
  if (!isPlainObject(option)) return option;

  return {
    ...option,
    color: [...token.chartPalette],
    textStyle: { ...(isPlainObject(option.textStyle) ? option.textStyle : {}), color: token.colorText },
    title: isPlainObject(option.title)
      ? {
          ...option.title,
          textStyle: {
            ...(isPlainObject(option.title.textStyle) ? option.title.textStyle : {}),
            color: token.colorText,
          },
        }
      : option.title,
    // 暗色下 ECharts 默认的白底 tooltip 会闪一下白光；底色/描边/文字都跟主题走
    tooltip: {
      ...(isPlainObject(option.tooltip) ? option.tooltip : {}),
      backgroundColor: token.colorBgContainer,
      borderColor: token.colorBorder,
      textStyle: {
        ...(isPlainObject(option.tooltip) && isPlainObject(option.tooltip.textStyle)
          ? option.tooltip.textStyle
          : {}),
        color: token.colorText,
      },
    },
    xAxis: withAxisThemeAll(option.xAxis, token),
    yAxis: withAxisThemeAll(option.yAxis, token),
  };
}
