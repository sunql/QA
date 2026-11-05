import { describe, it, expect } from "vitest";
import {
  toMeasureSpec,
  fromMeasureSpec,
  resolveDenominatorMeasure,
  needsDenominatorMeasure,
  sanitizeMeasureSpec,
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

/** 库里的历史分母（回填脚本写入，带 class_id / class_alias）。 */
const historicalDenominator = {
  agg: "COUNT",
  expr: "ORDER_ID",
  filter: null,
  class_id: 8,
  class_alias: "OrderItem",
};

describe("needsDenominatorMeasure", () => {
  it("比率 / 差值需要分母", () => {
    expect(needsDenominatorMeasure("RATIO")).toBe(true);
    expect(needsDenominatorMeasure("DIFFERENCE")).toBe(true);
  });

  it("度量型不需要分母（表单不渲染分母行）", () => {
    expect(needsDenominatorMeasure("MEASURE")).toBe(false);
    expect(needsDenominatorMeasure(undefined)).toBe(false);
  });
});

describe("resolveDenominatorMeasure", () => {
  it("表单未暴露分母行（MEASURE）⇒ 原样回传历史分母，不清空", () => {
    // 表单没渲染分母 ⇒ 字段未注册 ⇒ 表单值是 undefined，
    // 若当成「用户清空」提交 null，后端整块替换 JSONB ⇒ 历史分母被抹掉。
    const result = resolveDenominatorMeasure(
      {
        metricType: "MEASURE",
        originalMetricType: "MEASURE",
        agg: undefined,
        expr: undefined,
        filter: undefined,
      },
      historicalDenominator,
    );
    expect(result).toEqual(historicalDenominator);
  });

  it("表单未暴露分母行且库里没有分母 ⇒ null", () => {
    const result = resolveDenominatorMeasure(
      {
        metricType: "MEASURE",
        originalMetricType: "MEASURE",
        agg: undefined,
        expr: undefined,
        filter: undefined,
      },
      null,
    );
    expect(result).toBeNull();
  });

  it("把比率改成度量型 ⇒ 改型本身就是「不要分母」的意图 ⇒ 显式清空", () => {
    // 原类型 RATIO ⇒ 分母行本来是渲染的；改成 MEASURE 后行被卸载，
    // validateFields 不再返回分母字段。此时若「原样回传」，库里会留下一个
    // 用户看不见也清不掉的残留分母（发布闸门会因它对不上 formula 而 409）。
    const result = resolveDenominatorMeasure(
      {
        metricType: "MEASURE",
        originalMetricType: "RATIO",
        agg: undefined,
        expr: undefined,
        filter: undefined,
      },
      historicalDenominator,
    );
    expect(result).toBeNull();
  });

  it("把比率改成度量型且库里本来没分母 ⇒ null", () => {
    const result = resolveDenominatorMeasure(
      {
        metricType: "MEASURE",
        originalMetricType: "DIFFERENCE",
        agg: undefined,
        expr: undefined,
        filter: undefined,
      },
      null,
    );
    expect(result).toBeNull();
  });

  it("表单暴露了分母行（比率）⇒ 表单值说了算（显式清空 ⇒ null）", () => {
    const result = resolveDenominatorMeasure(
      {
        metricType: "RATIO",
        originalMetricType: "RATIO",
        agg: undefined,
        expr: "ORDER_ID",
        filter: "",
      },
      historicalDenominator,
    );
    expect(result).toBeNull();
  });

  it("表单暴露了分母行（比率）⇒ 回传表单值并保留 class_id / class_alias", () => {
    const result = resolveDenominatorMeasure(
      {
        metricType: "RATIO",
        originalMetricType: "RATIO",
        agg: "count",
        expr: "ORDER_ID",
        filter: "",
      },
      historicalDenominator,
    );
    expect(result).toEqual({
      agg: "COUNT",
      expr: "ORDER_ID",
      filter: null,
      class_id: 8,
      class_alias: "OrderItem",
    });
  });

  it("度量型改成比率 ⇒ 分母行渲染了 ⇒ 表单值说了算", () => {
    const result = resolveDenominatorMeasure(
      {
        metricType: "RATIO",
        originalMetricType: "MEASURE",
        agg: "count",
        expr: "ORDER_ID",
        filter: "",
      },
      historicalDenominator,
    );
    expect(result).toEqual({
      agg: "COUNT",
      expr: "ORDER_ID",
      filter: null,
      class_id: 8,
      class_alias: "OrderItem",
    });
  });

  it("原样回传时返回新对象，不把 existing 的引用交出去（不可变）", () => {
    const result = resolveDenominatorMeasure(
      {
        metricType: "MEASURE",
        originalMetricType: "MEASURE",
        agg: undefined,
        expr: undefined,
        filter: undefined,
      },
      historicalDenominator,
    );
    expect(result).not.toBe(historicalDenominator);
    // 下游若原地改 payload，不该连带改到从接口拿到的指标对象。
    if (result) result.agg = "MUTATED";
    expect(historicalDenominator.agg).toBe("COUNT");
  });

  it("原样回传时裁剪到后端 schema 允许的 5 个键（extra=forbid ⇒ 多余键会 422）", () => {
    const dirty = { ...historicalDenominator, rogue_key: "x" } as never;
    const result = resolveDenominatorMeasure(
      {
        metricType: "MEASURE",
        originalMetricType: "MEASURE",
        agg: undefined,
        expr: undefined,
        filter: undefined,
      },
      dirty,
    );
    expect(result).toEqual({
      agg: "COUNT",
      expr: "ORDER_ID",
      filter: null,
      class_id: 8,
      class_alias: "OrderItem",
    });
    expect(Object.keys(result ?? {}).sort()).toEqual([
      "agg",
      "class_alias",
      "class_id",
      "expr",
      "filter",
    ]);
  });
});

describe("sanitizeMeasureSpec", () => {
  it("空值 ⇒ null", () => {
    expect(sanitizeMeasureSpec(null)).toBeNull();
    expect(sanitizeMeasureSpec(undefined)).toBeNull();
  });

  it("字段类型不对时降级为 null（不让整个保存 422）", () => {
    const result = sanitizeMeasureSpec({
      agg: "COUNT",
      expr: 123 as never,
      filter: null,
      class_id: "8" as never,
      class_alias: null,
    });
    expect(result).toEqual({
      agg: "COUNT",
      expr: null,
      filter: null,
      class_id: null,
      class_alias: null,
    });
  });
});
