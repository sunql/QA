/** 发现运行的表选择器过滤逻辑（feat/discovery-skip-unprofilable-tables）。
 *
 * 钉住两个用户诉求：
 * 1. **不显示**不可剖析的表（Oracle 回收站对象等）；
 * 2. **能搜**：按表名 / 属主.表名 / **列名** 都能找到表（「排查包含某些列的表」）。
 */

import { describe, it, expect } from "vitest";

import {
  buildDiscoveryTableOptions,
  matchTableOption,
  type DiscoveryTableOption,
} from "../utils/discoveryTableFilter";

const RECYCLE_BIN_TABLE = "BIN$W81vJmasuczgY0bNqMBvog==$0";

const OPTION: DiscoveryTableOption = {
  value: "SUPPLIER",
  label: "THBI.SUPPLIER",
  columns: ["BPSNUM_0", "QTY"],
};

describe("matchTableOption", () => {
  it("空查询 ⇒ 命中且无列命中", () => {
    expect(matchTableOption(OPTION, "")).toEqual({ matched: true, columnHit: null });
    expect(matchTableOption(OPTION, "   ")).toEqual({ matched: true, columnHit: null });
  });

  it("按表名命中（大小写不敏感的包含匹配）", () => {
    expect(matchTableOption(OPTION, "supplier")).toEqual({
      matched: true,
      columnHit: null,
    });
  });

  it("按属主.表名命中", () => {
    expect(matchTableOption(OPTION, "THBI.")).toEqual({
      matched: true,
      columnHit: null,
    });
  });

  it("按**列名**命中 ⇒ 标出是哪一列", () => {
    expect(matchTableOption(OPTION, "qty")).toEqual({
      matched: true,
      columnHit: "QTY",
    });
  });

  it("命中不到 ⇒ 不命中", () => {
    expect(matchTableOption(OPTION, "orders").matched).toBe(false);
  });
});

describe("buildDiscoveryTableOptions", () => {
  const tables = [
    {
      tableName: "SUPPLIER",
      owner: "THBI",
      columns: [{ columnName: "QTY" }],
    },
    {
      tableName: RECYCLE_BIN_TABLE,
      owner: "THBI",
      columns: [{ columnName: "COL1" }],
    },
  ];

  it("不可剖析的表被隐藏，其余进选项", () => {
    const { options, hiddenTableNames } = buildDiscoveryTableOptions(tables, [
      RECYCLE_BIN_TABLE,
    ]);
    expect(options.map((option) => option.value)).toEqual(["SUPPLIER"]);
    expect(hiddenTableNames).toEqual([RECYCLE_BIN_TABLE]);
  });

  it("选项带属主前缀与列名（供「按列名搜表」）", () => {
    const { options } = buildDiscoveryTableOptions(tables, []);
    const supplier = options.find((option) => option.value === "SUPPLIER");
    expect(supplier?.label).toBe("THBI.SUPPLIER");
    expect(supplier?.columns).toEqual(["QTY"]);
  });

  it("隐藏判定大小写不敏感", () => {
    const { options, hiddenTableNames } = buildDiscoveryTableOptions(tables, [
      RECYCLE_BIN_TABLE.toLowerCase(),
    ]);
    expect(options.map((option) => option.value)).toEqual(["SUPPLIER"]);
    expect(hiddenTableNames).toEqual([RECYCLE_BIN_TABLE]);
  });

  it("没有不可剖析表 ⇒ 不隐藏任何表、隐藏清单为空", () => {
    const { options, hiddenTableNames } = buildDiscoveryTableOptions(tables, []);
    expect(options).toHaveLength(2);
    expect(hiddenTableNames).toEqual([]);
  });

  it("无属主时 label 就是表名", () => {
    const { options } = buildDiscoveryTableOptions(
      [{ tableName: "ITEM", columns: [] }],
      [],
    );
    expect(options[0].label).toBe("ITEM");
  });
});
