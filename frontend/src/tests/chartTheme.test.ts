/** 图表主题注入（决策 6）：服务端只发结构，颜色一律由前端主题层补。

两条契约：
1. **不可变**：`applyChartTheme` 返回新对象，绝不改传入的 option —— 传入的是
   store 里的消息对象（同一份被多处引用），原地改会串台。
2. **只补色，不改结构**：series/axis 的数据与类型一字不动，只往轴、文字、tooltip
   上补色。补错了顶多难看，动到结构就是画错了。
 */
import { describe, expect, it } from "vitest";
import { applyChartTheme } from "../theme/chartTheme";
import { DARK_TOKEN, LIGHT_TOKEN, type ThemeToken } from "../theme/tokens";

function makeToken(overrides: Partial<ThemeToken> = {}): ThemeToken {
  return { ...LIGHT_TOKEN, ...overrides };
}

const BAR_OPTION: Record<string, unknown> = {
  title: { text: "各供应商收货量" },
  xAxis: { type: "category", data: ["A", "B"] },
  yAxis: { type: "value" },
  series: [{ type: "bar", data: [1, 2] }],
};

describe("applyChartTheme", () => {
  it("注入分类系列色板（服务端不发 color）", () => {
    const token = makeToken({ chartPalette: ["#111111", "#222222"] });

    expect(applyChartTheme(BAR_OPTION, token).color).toEqual(["#111111", "#222222"]);
  });

  it("不改动传入的 option（不可变）", () => {
    const token = makeToken({ chartPalette: ["#111111"] });
    const before = JSON.stringify(BAR_OPTION);

    const themed = applyChartTheme(BAR_OPTION, token);

    expect(themed).not.toBe(BAR_OPTION);
    expect(JSON.stringify(BAR_OPTION)).toBe(before);
  });

  it("补轴色与文字色，series 数据/类型一字不动", () => {
    const token = makeToken({ colorBorder: "#abcdef", colorTextTertiary: "#123456" });

    const themed = applyChartTheme(BAR_OPTION, token) as {
      xAxis: Record<string, unknown>;
      yAxis: Record<string, unknown>;
      series: unknown;
    };

    expect((themed.xAxis.axisLine as Record<string, unknown>).lineStyle).toEqual({
      color: "#abcdef",
    });
    expect((themed.xAxis.axisLabel as Record<string, unknown>).color).toBe("#123456");
    // 类目轴不该出现分割线（只在数值轴补）
    expect(themed.xAxis.splitLine).toBeUndefined();
    expect((themed.yAxis.splitLine as Record<string, unknown>).lineStyle).toEqual({
      color: "#abcdef",
    });
    // 结构原样：数据与类型未被触碰
    expect(themed.series).toEqual([{ type: "bar", data: [1, 2] }]);
  });

  it("轴 lineStyle 的其它键（虚线/宽度）不被颜色覆盖", () => {
    const token = makeToken({ colorBorder: "#abcdef" });
    const option = {
      xAxis: { type: "category", axisLine: { lineStyle: { type: "dashed", width: 2 } } },
      yAxis: { type: "value", splitLine: { lineStyle: { type: "dotted" } } },
    };

    const themed = applyChartTheme(option, token) as {
      xAxis: Record<string, unknown>;
      yAxis: Record<string, unknown>;
    };

    expect((themed.xAxis.axisLine as Record<string, unknown>).lineStyle).toEqual({
      type: "dashed",
      width: 2,
      color: "#abcdef",
    });
    expect((themed.yAxis.splitLine as Record<string, unknown>).lineStyle).toEqual({
      type: "dotted",
      color: "#abcdef",
    });
  });

  it("支持数组形式的轴（双轴组合图）", () => {
    const token = makeToken({ colorBorder: "#abcdef" });
    const option = { xAxis: [{ type: "category" }], yAxis: [{ type: "value" }, { type: "value" }] };

    const themed = applyChartTheme(option, token) as { yAxis: Record<string, unknown>[] };

    expect(themed.yAxis).toHaveLength(2);
    for (const axis of themed.yAxis) {
      expect((axis.axisLine as Record<string, unknown>).lineStyle).toEqual({ color: "#abcdef" });
    }
  });

  it("饼图/热力图这类没有轴的对象也能注入，不抛错", () => {
    const token = makeToken();
    const option = { series: [{ type: "pie", data: [{ name: "A", value: 1 }] }] };

    const themed = applyChartTheme(option, token) as Record<string, unknown>;

    expect(themed.color).toEqual(token.chartPalette);
    expect(themed.series).toEqual(option.series);
  });

  it("文字色同时补到全局 textStyle 与标题", () => {
    const token = makeToken({ colorText: "#000001" });

    const themed = applyChartTheme(BAR_OPTION, token) as {
      textStyle: Record<string, unknown>;
      title: Record<string, unknown>;
    };

    expect(themed.textStyle.color).toBe("#000001");
    expect((themed.title.textStyle as Record<string, unknown>).color).toBe("#000001");
    // 标题文字本身未被改写
    expect(themed.title.text).toBe("各供应商收货量");
  });

  it("tooltip 底色/描边/文字走主题（暗色下默认白底会刺眼）", () => {
    const token = makeToken({
      colorBgContainer: "#152838",
      colorBorder: "#1f3a52",
      colorText: "#FFFFFF",
    });

    const themed = applyChartTheme(BAR_OPTION, token) as { tooltip: Record<string, unknown> };

    expect(themed.tooltip.backgroundColor).toBe("#152838");
    expect(themed.tooltip.borderColor).toBe("#1f3a52");
    expect((themed.tooltip.textStyle as Record<string, unknown>).color).toBe("#FFFFFF");
  });

  it("保留 option 里已有的 tooltip 配置（只补色）", () => {
    const token = makeToken();
    const option = { ...BAR_OPTION, tooltip: { trigger: "axis" } };

    const themed = applyChartTheme(option, token) as { tooltip: Record<string, unknown> };

    expect(themed.tooltip.trigger).toBe("axis");
  });

  it("两个主题各给一套调色板（暗色不套亮色）", () => {
    expect(DARK_TOKEN.chartPalette.length).toBeGreaterThanOrEqual(6);
    expect(LIGHT_TOKEN.chartPalette.length).toBeGreaterThanOrEqual(6);
    expect(DARK_TOKEN.chartPalette).not.toEqual(LIGHT_TOKEN.chartPalette);
  });

  it("非法输入原样返回（系统边界：option 来自后端 JSON）", () => {
    const token = makeToken();
    const notAnObject = "oops" as unknown as Record<string, unknown>;

    expect(applyChartTheme(notAnObject, token)).toBe(notAnObject);
  });
});
