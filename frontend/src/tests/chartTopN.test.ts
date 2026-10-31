import { describe, expect, it } from "vitest";
import { defaultChartOption, topNMetaOf, topNOptionOf } from "../utils/chartTopN";

const META = { limit: 20, hidden: 5, total: 25 };

function fullView() {
  return {
    yAxis: { type: "category", data: Array.from({ length: 25 }, (_, i) => `S${i}`) },
    series: [{ type: "bar" }],
  };
}

function truncatedView() {
  return {
    yAxis: { type: "category", data: Array.from({ length: 21 }, (_, i) => `S${i}`) },
    series: [{ type: "bar" }],
  };
}

function optionWithTopN(overrides: Record<string, unknown> = {}) {
  return {
    ...fullView(),
    topNOption: truncatedView(),
    topNMeta: { ...META },
    ...overrides,
  };
}

describe("topNMetaOf", () => {
  it("读取后端下发的 limit/hidden/total", () => {
    expect(topNMetaOf(optionWithTopN())).toEqual(META);
  });

  it("缺 meta 时返回 null（老会话、非排名图都走这里）", () => {
    expect(topNMetaOf(fullView())).toBeNull();
    expect(topNMetaOf(null)).toBeNull();
    expect(topNMetaOf(undefined)).toBeNull();
  });

  it("meta 字段缺失或非数值时整体判无效，不猜", () => {
    for (const meta of [
      { limit: 20, hidden: 5 },
      { limit: 20, hidden: 5, total: "25" },
      { limit: 20, hidden: -1, total: 25 },
      { limit: 0, hidden: 5, total: 25 },
      { limit: 20, hidden: 5, total: 19 },
    ]) {
      expect(topNMetaOf({ ...fullView(), topNMeta: meta }), JSON.stringify(meta)).toBeNull();
    }
  });
});

describe("topNOptionOf", () => {
  it("meta 与截断视图成对出现时返回截断视图", () => {
    expect(topNOptionOf(optionWithTopN())).toEqual(truncatedView());
  });

  it("只有 topNOption 没有 meta 时不认（半份负载不当真）", () => {
    const orphan = { ...fullView(), topNOption: truncatedView() };

    expect(topNOptionOf(orphan)).toBeNull();
  });

  it("topNOption 不是普通对象时不认", () => {
    for (const topNOption of [null, [], "x", 3]) {
      expect(topNOptionOf(optionWithTopN({ topNOption }))).toBeNull();
    }
  });
});

describe("defaultChartOption", () => {
  it("有截断视图时默认给截断视图（用户先看前 20）", () => {
    expect(defaultChartOption(optionWithTopN())).toEqual(truncatedView());
  });

  it("无截断视图时原样返回同一个对象（不复制、不改写）", () => {
    const full = fullView();

    expect(defaultChartOption(full)).toBe(full);
  });

  it("不修改入参（不可变路径）", () => {
    const option = optionWithTopN();
    const snapshot = JSON.parse(JSON.stringify(option));

    defaultChartOption(option);

    expect(option).toEqual(snapshot);
  });
});
