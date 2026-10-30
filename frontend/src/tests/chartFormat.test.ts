/** 图表数字格式化（主题表现层）：轴缩写（万/亿）+ tooltip 千分位。

背景：后端只发 JSON（无法下发 JS formatter 函数），大数值此前裸奔 ——
1234567 直接挤爆数值轴。格式化是**表现**不是**结构**，所以落在前端主题层：
`applyChartNumberFormat` 只在缺省时注入 formatter，已有 formatter（函数或
字符串）一律不动；series 不被遍历，饼图 `{b}: {d}%`、瀑布图 `{c}` 保持逐字。

两条契约：
1. **不可变**：返回新对象，传入的 option 来自 store，原地改会串台。
2. **只管数字，不猜语义**：不做百分比推断、不发明 K/M（zh 语境用 万/亿）。
 */
import { describe, expect, it } from "vitest";
import {
  applyChartNumberFormat,
  formatAxisValue,
  formatTooltipValue,
} from "../theme/chartFormat";

describe("formatAxisValue", () => {
  it("万以下走千分位", () => {
    expect(formatAxisValue(9812)).toBe("9,812");
    expect(formatAxisValue(0)).toBe("0");
    expect(formatAxisValue(12.3456)).toBe("12.35");
  });

  it("万/亿缩写（zh-CN）", () => {
    expect(formatAxisValue(15000)).toBe("1.5万");
    expect(formatAxisValue(100_000_000)).toBe("1亿");
    expect(formatAxisValue(123_456_789)).toBe("1.23亿");
    expect(formatAxisValue(-15000)).toBe("-1.5万");
  });

  it("非有限数返回空串（ECharts 跳过空标签）", () => {
    expect(formatAxisValue(Number.NaN)).toBe("");
    expect(formatAxisValue(Number.POSITIVE_INFINITY)).toBe("");
    expect(formatAxisValue("not-a-number")).toBe("");
    expect(formatAxisValue(null)).toBe("");
  });

  it("非 zh locale 不缩写，只千分位", () => {
    expect(formatAxisValue(15000, "en-US")).toBe("15,000");
  });
});

describe("formatTooltipValue", () => {
  it("千分位 + 最多两位小数", () => {
    expect(formatTooltipValue(1_234_567.891)).toBe("1,234,567.89");
    expect(formatTooltipValue(9812)).toBe("9,812");
    expect(formatTooltipValue(12.5)).toBe("12.5");
    expect(formatTooltipValue(-0.126)).toBe("-0.13");
  });

  it("数组递归连接（双轴 tooltip 的多值形态）", () => {
    expect(formatTooltipValue([9812, 98120])).toBe("9,812, 98,120");
  });

  it("null/undefined → 空串；非数值原样字符串化", () => {
    expect(formatTooltipValue(null)).toBe("");
    expect(formatTooltipValue(undefined)).toBe("");
    expect(formatTooltipValue("2026-01")).toBe("2026-01");
  });
});

describe("applyChartNumberFormat", () => {
  const OPTION: Record<string, unknown> = {
    xAxis: { type: "category", data: ["A", "B"] },
    yAxis: { type: "value" },
    tooltip: { trigger: "axis" },
    series: [{ type: "bar", data: [1, 2] }],
  };

  it("数值轴注入 axisLabel.formatter，类目轴不动", () => {
    const formatted = applyChartNumberFormat(OPTION);

    const yAxis = formatted.yAxis as Record<string, unknown>;
    const axisLabel = yAxis.axisLabel as Record<string, unknown>;
    const formatter = axisLabel.formatter as (value: unknown) => string;
    expect(typeof formatter).toBe("function");
    expect(formatter(15000)).toBe("1.5万");

    const xAxis = formatted.xAxis as Record<string, unknown>;
    expect((xAxis.axisLabel as Record<string, unknown> | undefined)?.formatter).toBeUndefined();
  });

  it("tooltip 注入 valueFormatter", () => {
    const tooltip = applyChartNumberFormat(OPTION).tooltip as Record<string, unknown>;
    const valueFormatter = tooltip.valueFormatter as (value: unknown) => string;
    expect(typeof valueFormatter).toBe("function");
    expect(valueFormatter(9812)).toBe("9,812");
  });

  it("已有 formatter（函数/字符串）不被覆盖", () => {
    const existing = (value: unknown) => `${value}`;
    const withFn = applyChartNumberFormat({
      ...OPTION,
      yAxis: { type: "value", axisLabel: { formatter: existing } },
    });
    expect(
      ((withFn.yAxis as Record<string, unknown>).axisLabel as Record<string, unknown>).formatter
    ).toBe(existing);

    const withString = applyChartNumberFormat({
      ...OPTION,
      yAxis: { type: "value", axisLabel: { formatter: "{value}元" } },
    });
    expect(
      ((withString.yAxis as Record<string, unknown>).axisLabel as Record<string, unknown>).formatter
    ).toBe("{value}元");
  });

  it("已有 tooltip.valueFormatter 不被覆盖", () => {
    const existing = (value: unknown) => `${value}`;
    const formatted = applyChartNumberFormat({
      ...OPTION,
      tooltip: { trigger: "axis", valueFormatter: existing },
    });
    expect((formatted.tooltip as Record<string, unknown>).valueFormatter).toBe(existing);
  });

  it("双轴数组全覆盖", () => {
    const formatted = applyChartNumberFormat({
      ...OPTION,
      yAxis: [{ type: "value" }, { type: "value" }],
    });
    for (const axis of formatted.yAxis as Record<string, unknown>[]) {
      expect(typeof ((axis.axisLabel as Record<string, unknown>).formatter as unknown)).toBe(
        "function"
      );
    }
  });

  it("非 value/log 轴不注入", () => {
    const formatted = applyChartNumberFormat({
      ...OPTION,
      yAxis: { type: "log" },
      xAxis2: { type: "time" },
    });
    expect(
      typeof (((formatted.yAxis as Record<string, unknown>).axisLabel as Record<string, unknown>)
        .formatter as unknown)
    ).toBe("function");
  });

  it("不可变；非对象原样返回", () => {
    const before = JSON.stringify(OPTION);
    const formatted = applyChartNumberFormat(OPTION);
    expect(formatted).not.toBe(OPTION);
    expect(JSON.stringify(OPTION)).toBe(before);

    const weird = "not-an-object" as unknown as Record<string, unknown>;
    expect(applyChartNumberFormat(weird)).toBe(weird);
  });
});
