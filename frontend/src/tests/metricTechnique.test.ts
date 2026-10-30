import { describe, it, expect } from "vitest";
import {
  toMeasureSpec,
  fromMeasureSpec,
  EMPTY_MEASURE_FORM,
} from "../utils/metricTechnique";

describe("toMeasureSpec", () => {
  it("缺 agg ⇒ null（显式清空）", () => {
    expect(toMeasureSpec({ agg: undefined, expr: "QTY", filter: "" })).toBeNull();
  });

  it("agg 归一为大写", () => {
    const m = toMeasureSpec({ agg: "sum", expr: "QTY", filter: "" });
    expect(m?.agg).toBe("SUM");
  });

  it("空表达式 ⇒ expr 归一为 null", () => {
    // 有 filter ⇒ measure 仍有效；空白 expr 归一成 null。
    const m = toMeasureSpec({ agg: "SUM", expr: "  ", filter: " STATUS='OK' " });
    expect(m?.expr).toBeNull();
    expect(m?.filter).toBe("STATUS='OK'");
  });

  it("既无 expr 又无 filter ⇒ null（显式清空）", () => {
    expect(toMeasureSpec({ agg: "SUM", expr: "", filter: "" })).toBeNull();
  });

  it("expr / filter 为 undefined（字段未注册）⇒ null（不抛错）", () => {
    expect(
      toMeasureSpec({ agg: "SUM", expr: undefined, filter: undefined }),
    ).toBeNull();
  });

  it("只填 filter 也算有效 measure", () => {
    const m = toMeasureSpec({ agg: "COUNT", expr: "", filter: "STATUS='OK'" });
    expect(m?.filter).toBe("STATUS='OK'");
  });

  it("保留 existing 的 class_id / class_alias（不抹掉回填脚本写入的源类信息）", () => {
    const m = toMeasureSpec(
      { agg: "SUM", expr: "QTY", filter: "" },
      { agg: "SUM", expr: "OLD", filter: null, class_id: 7, class_alias: "Order" },
    );
    expect(m?.class_id).toBe(7);
    expect(m?.class_alias).toBe("Order");
  });
});

describe("fromMeasureSpec", () => {
  it("非对象 ⇒ 空表单", () => {
    expect(fromMeasureSpec(null)).toEqual(EMPTY_MEASURE_FORM);
    expect(fromMeasureSpec("x")).toEqual(EMPTY_MEASURE_FORM);
  });

  it("缺字段 ⇒ 空字符串", () => {
    expect(fromMeasureSpec({ agg: "SUM" })).toEqual({
      agg: "SUM",
      expr: "",
      filter: "",
    });
  });

  it("往返一致", () => {
    const spec = { agg: "SUM", expr: "QTY", filter: null, class_id: 1, class_alias: "T" };
    const back = toMeasureSpec(fromMeasureSpec(spec));
    expect(back?.agg).toBe("SUM");
    expect(back?.expr).toBe("QTY");
    expect(back?.filter).toBeNull();
  });
});
