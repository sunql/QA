import { describe, expect, it } from "vitest";
import { chartHeightOf } from "../utils/chartLayout";

function hbarOption(count: number, withLegend = false) {
  return {
    yAxis: {
      type: "category",
      data: Array.from({ length: count }, (_, i) => `S${i}`),
    },
    xAxis: { type: "value" },
    series: [{ type: "bar", data: count }],
    ...(withLegend ? { legend: { show: true, type: "scroll" } } : {}),
  };
}

function heatmapOption(rowCount: number) {
  return {
    xAxis: { type: "category", data: ["A", "B"] },
    yAxis: {
      type: "category",
      data: Array.from({ length: rowCount }, (_, i) => `R${i}`),
    },
    series: [{ type: "heatmap" }],
  };
}

describe("chartHeightOf", () => {
  it("类别很少时保持 320 基线", () => {
    expect(chartHeightOf(hbarOption(3))).toBe(320);
    expect(chartHeightOf(hbarOption(8))).toBe(320);
  });

  it("横向条形图按类别数增高（96 + 28/类）", () => {
    expect(chartHeightOf(hbarOption(20))).toBe(656);
    expect(chartHeightOf(hbarOption(30))).toBe(936);
  });

  it("有图例时额外留 32px", () => {
    expect(chartHeightOf(hbarOption(20, true))).toBe(688);
  });

  it("超过 960 封顶（靠 dataZoom 看剩余类别）", () => {
    expect(chartHeightOf(hbarOption(60))).toBe(960);
  });

  it("热力图按行数增高（120 + 26/行）", () => {
    expect(chartHeightOf(heatmapOption(10))).toBe(380);
    expect(chartHeightOf(heatmapOption(40))).toBe(960);
  });

  it("纵向柱状图/折线/饼图保持 320", () => {
    expect(
      chartHeightOf({
        xAxis: { type: "category", data: ["A", "B", "C"] },
        yAxis: { type: "value" },
        series: [{ type: "bar" }],
      })
    ).toBe(320);
  });

  it("没有 option / 没有类目轴时回落 320（反向守卫）", () => {
    expect(chartHeightOf(null)).toBe(320);
    expect(chartHeightOf(undefined)).toBe(320);
    expect(chartHeightOf({})).toBe(320);
    expect(chartHeightOf({ series: [] })).toBe(320);
    expect(chartHeightOf(hbarOption(0))).toBe(320);
  });
});
