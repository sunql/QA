/** Task 14：证据面板 summary 结构化部分的纯函数（topN / timeRange）。
 *
 * 这些函数**只产出表 / 标签形状**，不画图、不猜度量列（后端 `EvidenceSummary` 不导出
 * `metricColumn` ⇒ 客户端无从断定哪列是「值」）。纯函数：不读 store / 不改入参。
 */
import { describe, expect, it } from "vitest";

import { timeRangeLabels, topNTableShape } from "../components/research/evidenceSummary";

describe("topNTableShape", () => {
  it("null / 空 summary ⇒ null", () => {
    expect(topNTableShape(null)).toBeNull();
    expect(topNTableShape({})).toBeNull();
  });

  it("无 topN ⇒ null", () => {
    expect(topNTableShape({ total: "1", baseline: "0" })).toBeNull();
  });

  it("topN 非线性值 ⇒ null（不炸）", () => {
    expect(topNTableShape({ topN: "nope" })).toBeNull();
    expect(topNTableShape({ topN: { rank: 1 } })).toBeNull();
  });

  it("列 = rank + 各行键并集（按首次出现顺序），值字符串化", () => {
    const shape = topNTableShape({
      topN: [
        { rank: 1, row: { B: "x", A: "1" } },
        { rank: 2, row: { A: "2", C: "3" } },
      ],
    });
    expect(shape?.columns).toEqual(["rank", "B", "A", "C"]);
    expect(shape?.rows).toEqual([
      { rank: "1", B: "x", A: "1" },
      { rank: "2", A: "2", C: "3" },
    ]);
  });

  it("跳过缺 row / 非对象项；全无有效行 ⇒ null", () => {
    expect(topNTableShape({ topN: [{ rank: 1 }, null, "junk"] })).toBeNull();
  });

  it("null 单元格 ⇒ 空串", () => {
    const shape = topNTableShape({ topN: [{ rank: 1, row: { A: null } }] });
    expect(shape?.rows[0].A).toBe("");
  });

  it("不修改传入 summary", () => {
    const summary = { topN: [{ rank: 1, row: { A: "1" } }] };
    const snapshot = JSON.stringify(summary);
    topNTableShape(summary);
    expect(JSON.stringify(summary)).toBe(snapshot);
  });
});

describe("timeRangeLabels", () => {
  it("合法 timeRange ⇒ { start, end }", () => {
    expect(timeRangeLabels({ timeRange: { start: "a", end: "b" } })).toEqual({ start: "a", end: "b" });
  });

  it("缺省 / 非法 ⇒ null", () => {
    expect(timeRangeLabels(null)).toBeNull();
    expect(timeRangeLabels({})).toBeNull();
    expect(timeRangeLabels({ timeRange: { start: "a" } })).toBeNull();
    expect(timeRangeLabels({ timeRange: { start: 1, end: 2 } })).toBeNull();
    expect(timeRangeLabels({ timeRange: "x" })).toBeNull();
  });

  it("不修改传入 summary", () => {
    const summary = { timeRange: { start: "a", end: "b" } };
    const snapshot = JSON.stringify(summary);
    timeRangeLabels(summary);
    expect(JSON.stringify(summary)).toBe(snapshot);
  });
});
