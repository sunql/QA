import { describe, it, expect } from "vitest";
import {
  contains,
  matchSelect,
  matchesAny,
  filterClasses,
  filterProperties,
  filterMetrics,
  filterJoins,
} from "../utils/ontologyFilter";
import type {
  OntologyClass,
  OntologyProperty,
  OntologyMetric,
  OntologyJoin,
} from "../types/ontology";

// 行数据工厂：只关心过滤字段，其余填默认值
function cls(over: Partial<OntologyClass>): OntologyClass {
  return {
    id: 1,
    className: "PRECEIPT",
    classAlias: null,
    description: null,
    sourceTable: null,
    parentClassId: null,
    createdBy: null,
    createdTime: null,
    updatedTime: null,
    version: 1,
    validFrom: null,
    validTo: null,
    ...over,
  };
}

function prop(over: Partial<OntologyProperty>): OntologyProperty {
  return {
    id: 1,
    classId: 1,
    propertyName: "P1",
    propertyAlias: null,
    dataType: "STRING",
    isPrimaryKey: false,
    isForeignKey: false,
    refClassId: null,
    sourceColumn: null,
    createdTime: null,
    updatedTime: null,
    ...over,
  };
}

function metric(over: Partial<OntologyMetric>): OntologyMetric {
  return {
    id: 1,
    metricName: "M1",
    metricAlias: null,
    formula: "SUM(x)",
    aggFunction: "SUM",
    targetClassId: null,
    dimensionDefaults: null,
    createdBy: null,
    createdTime: null,
    updatedTime: null,
    ...over,
  };
}

function join(over: Partial<OntologyJoin>): OntologyJoin {
  return {
    id: 1,
    sourceClassId: 1,
    sourceColumns: ["A_0"],
    targetClassId: 2,
    targetColumns: ["B_0"],
    joinType: "INNER",
    relationType: "business",
    description: null,
    joinKey: "k",
    createdBy: null,
    createdTime: null,
    updatedTime: null,
    ...over,
  };
}

describe("ontologyFilter 纯函数", () => {
  describe("contains / matchSelect / matchesAny", () => {
    it("空关键字视为不过滤", () => {
      expect(contains("X", "")).toBe(true);
      expect(matchSelect("", "X")).toBe(true);
      expect(matchesAny(["a"], "")).toBe(true);
    });

    it("大小写不敏感子串匹配", () => {
      expect(contains("PurchaseOrder", "purchase")).toBe(true);
      expect(contains("PORDERQ", "order")).toBe(true);
      expect(contains("PORDERQ", "XYZ")).toBe(false);
    });

    it("null / undefined 值按空串处理", () => {
      expect(contains(null, "x")).toBe(false);
      expect(contains(undefined, "x")).toBe(false);
      expect(contains(null, "")).toBe(true);
    });

    it("matchSelect 精确匹配", () => {
      expect(matchSelect("3", "3")).toBe(true);
      expect(matchSelect("3", "33")).toBe(false);
    });

    it("matchesAny 任一元素命中", () => {
      expect(matchesAny(["BPTNUM_0", "X"], "bpt")).toBe(true);
      expect(matchesAny(["A"], "Z")).toBe(false);
      expect(matchesAny(null, "x")).toBe(false);
    });
  });

  describe("filterClasses", () => {
    const rows = [
      cls({ id: 1, className: "PRECEIPT", classAlias: "收货单", sourceTable: "ZJTH.PRECEIPT", description: "采购收货" }),
      cls({ id: 2, className: "PORDERQ", classAlias: "采购订单", sourceTable: "ZJTH.PORDERQ", description: "订单明细" }),
    ];

    it("空 filters 返回全部", () => {
      expect(filterClasses(rows, {})).toHaveLength(2);
    });

    it("按类名子串过滤", () => {
      expect(filterClasses(rows, { className: "PORDER" }).map((r) => r.className)).toEqual(["PORDERQ"]);
    });

    it("多条件 AND（别名 + 数据表）", () => {
      expect(filterClasses(rows, { classAlias: "采购", sourceTable: "ORDERQ" }).map((r) => r.className)).toEqual([
        "PORDERQ",
      ]);
    });

    it("多条件 AND 不满足时为空", () => {
      expect(filterClasses(rows, { classAlias: "收货", sourceTable: "ORDERQ" })).toEqual([]);
    });

    it("大小写不敏感", () => {
      expect(filterClasses(rows, { className: "preceipt" }).map((r) => r.id)).toEqual([1]);
    });
  });

  describe("filterProperties", () => {
    const rows = [
      prop({ id: 1, classId: 1, propertyName: "物料编码", propertyAlias: "ITMREF", sourceColumn: "ITMREF_0", dataType: "STRING" }),
      prop({ id: 2, classId: 1, propertyName: "数量", propertyAlias: "QTY", sourceColumn: "QTY_0", dataType: "DECIMAL" }),
      prop({ id: 3, classId: 2, propertyName: "单价", propertyAlias: "PRI", sourceColumn: "PRI_0", dataType: "DECIMAL" }),
    ];

    it("空 filters 返回全部", () => {
      expect(filterProperties(rows, {})).toHaveLength(3);
    });

    it("按属性名过滤", () => {
      expect(filterProperties(rows, { propertyName: "单价" }).map((r) => r.id)).toEqual([3]);
    });

    it("按源字段过滤", () => {
      expect(filterProperties(rows, { sourceColumn: "PRI_0" }).map((r) => r.id)).toEqual([3]);
    });

    it("按所属类精确过滤", () => {
      expect(filterProperties(rows, { classId: "1" }).map((r) => r.id)).toEqual([1, 2]);
    });

    it("按数据类型精确过滤", () => {
      expect(filterProperties(rows, { dataType: "DECIMAL" }).map((r) => r.id)).toEqual([2, 3]);
    });

    it("多条件 AND（所属类 + 数据类型）", () => {
      expect(filterProperties(rows, { classId: "1", dataType: "DECIMAL" }).map((r) => r.id)).toEqual([2]);
    });
  });

  describe("filterMetrics", () => {
    const rows = [
      metric({ id: 1, metricName: "采购总额", metricAlias: "PUR_AMT", targetClassId: 1 }),
      metric({ id: 2, metricName: "收货数量", metricAlias: "RCV_QTY", targetClassId: 2 }),
    ];

    it("空 filters 返回全部", () => {
      expect(filterMetrics(rows, {})).toHaveLength(2);
    });

    it("按指标名过滤", () => {
      expect(filterMetrics(rows, { metricName: "总额" }).map((r) => r.id)).toEqual([1]);
    });

    it("按目标类精确过滤", () => {
      expect(filterMetrics(rows, { targetClassId: "2" }).map((r) => r.id)).toEqual([2]);
    });

    it("多条件 AND", () => {
      expect(filterMetrics(rows, { metricName: "数量", targetClassId: "2" }).map((r) => r.id)).toEqual([2]);
    });
  });

  describe("filterJoins", () => {
    const rows = [
      join({ id: 1, sourceClassId: 1, sourceColumns: ["BPTNUM_0"], targetClassId: 2, targetColumns: ["BPRNUM_0"] }),
      join({ id: 2, sourceClassId: 2, sourceColumns: ["ITMREF_0"], targetClassId: 3, targetColumns: ["PLICRI2_0"] }),
    ];

    it("空 filters 返回全部", () => {
      expect(filterJoins(rows, {})).toHaveLength(2);
    });

    it("按源类精确过滤", () => {
      expect(filterJoins(rows, { sourceClassId: "2" }).map((r) => r.id)).toEqual([2]);
    });

    it("按源列任一命中", () => {
      expect(filterJoins(rows, { sourceColumns: "ITMREF" }).map((r) => r.id)).toEqual([2]);
    });

    it("多条件 AND（源类 + 目标列）", () => {
      expect(filterJoins(rows, { sourceClassId: "1", targetColumns: "BPR" }).map((r) => r.id)).toEqual([1]);
    });
  });
});
