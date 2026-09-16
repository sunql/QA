import { describe, it, expect } from "vitest";
import type { TableSchema } from "../types/datasource";
import type {
  ImportRuleConfig,
  ImportPreviewResponse,
  ProposedClass,
  ProposedJoin,
} from "../types/localImport";
import {
  allColumnNames,
  buildExecuteRequest,
  chosenColumns,
  DEFAULT_JOIN_INFERENCE,
  joinKey,
  pruneColumnSubset,
  tableNameIndex,
  toPreviewRequest,
  toRulesWithJoinInference,
  validJoins,
  type ColumnSubset,
} from "../components/localImport/importSelection";

// ---- 数据工厂 ----
function table(name: string, columns: string[]): TableSchema {
  return {
    tableName: name,
    owner: "X3",
    columns: columns.map((columnName) => ({
      columnName,
      dataType: "VARCHAR",
      nullable: true,
    })),
    primaryKeys: columns.slice(0, 1),
    foreignKeys: [],
  };
}

const TABLES: TableSchema[] = [table("ITMMASTER", ["ITMREF_0", "ITMDES1_0"]), table("PORDER", ["POHNUM_0", "BPRNUM_0"]), table("PORDERQ", ["POHNUM_0", "LIN_0"])];
const INDEX = tableNameIndex(TABLES);

function rules(withJoin: boolean): ImportRuleConfig {
  return {
    tableFilter: {},
    joinInference: {
      inferDeclaredFk: withJoin,
      inferNameConvention: withJoin,
    },
  };
}

function join(sourceTable: string, sourceColumns: string[], targetTable: string, targetColumns: string[]): ProposedJoin {
  return {
    sourceTable,
    sourceColumns,
    targetTable,
    targetColumns,
    joinType: "INNER",
    relationType: "foreign_key",
    isSelected: true,
    inferredBy: "name_convention",
  };
}

function previewResponse(joins: ProposedJoin[]): ImportPreviewResponse {
  return {
    datasourceId: 1,
    proposedClasses: TABLES.map((tb): ProposedClass => ({
      sourceTable: tb.tableName,
      className: tb.tableName,
      classAlias: null,
      description: null,
      properties: [],
      isSelected: false,
    })),
    proposedJoins: joins,
    conflicts: [],
    filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
    llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
  };
}

describe("tableNameIndex / allColumnNames / chosenColumns", () => {
  it("index 按表名可达，未知表返回 undefined", () => {
    expect(INDEX.get("PORDER")?.tableName).toBe("PORDER");
    expect(INDEX.get("NOPE")).toBeUndefined();
  });

  it("allColumnNames 返回保持顺序的列名；空表返回 []", () => {
    expect(allColumnNames(INDEX.get("PORDERQ"))).toEqual(["POHNUM_0", "LIN_0"]);
    expect(allColumnNames(undefined)).toEqual([]);
  });

  it("chosenColumns：subset 无条目视为全列；空条目视为全列；有值返回该值", () => {
    expect(chosenColumns({}, "PORDER", ["A", "B"])).toEqual(["A", "B"]);
    expect(chosenColumns({ PORDER: [] }, "PORDER", ["A", "B"])).toEqual(["A", "B"]);
    expect(chosenColumns({ PORDER: ["A"] }, "PORDER", ["A", "B"])).toEqual(["A"]);
  });
});

describe("toRulesWithJoinInference", () => {
  it("合并开关并保留其余字段，不改入参", () => {
    const original = rules(true);
    const updated = toRulesWithJoinInference(original, {
      inferDeclaredFk: false,
      inferNameConvention: true,
    });
    expect(original.joinInference?.inferDeclaredFk).toBe(true);
    expect(updated.joinInference).toEqual({
      inferDeclaredFk: false,
      inferNameConvention: true,
    });
    expect(updated.tableFilter).toEqual({});
  });

  it("DEFAULT_JOIN_INFERENCE 两个推断都默认开", () => {
    expect(DEFAULT_JOIN_INFERENCE).toEqual({
      inferDeclaredFk: true,
      inferNameConvention: true,
    });
  });
});

describe("pruneColumnSubset", () => {
  it("丢弃未选表条目；真子集保留；全列/超集/空集条目丢弃", () => {
    const subset: ColumnSubset = {
      PORDER: ["POHNUM_0"], // 真子集 → 保留
      ITMMASTER: ["ITMREF_0", "ITMDES1_0"], // 全列 → 丢弃
      PORDERQ: ["FAKE"], // 非法列 → 丢弃
    };
    const pruned = pruneColumnSubset(subset, ["PORDER", "PORDERQ"], INDEX);
    expect(pruned).toEqual({ PORDER: ["POHNUM_0"] });
    // 不改入参
    expect(Object.keys(subset)).toHaveLength(3);
  });

  it("入参为空/未选表 → 返回空对象", () => {
    expect(pruneColumnSubset({}, [], INDEX)).toEqual({});
    expect(pruneColumnSubset({ PORDER: ["POHNUM_0"] }, [], INDEX)).toEqual({});
  });
});

describe("toPreviewRequest", () => {
  it("selectedTables 空数组 → 语义 undefined（后端全量预览）", () => {
    const request = toPreviewRequest(rules(true), [], {}, INDEX);
    expect(request.selectedTables).toBeUndefined();
    expect(request.selectedColumns).toBeUndefined();
  });

  it("含表选与真子集列选；全列表不出现在 selectedColumns", () => {
    const subset: ColumnSubset = {
      PORDER: ["POHNUM_0"],
      PORDERQ: ["POHNUM_0", "LIN_0"], // 全列 → 被 prune 掉
    };
    const request = toPreviewRequest(
      rules(true),
      ["PORDER", "PORDERQ"],
      subset,
      INDEX,
    );
    expect(request.selectedTables).toEqual(["PORDER", "PORDERQ"]);
    expect(request.selectedColumns).toEqual({ PORDER: ["POHNUM_0"] });
    expect(request.rules.joinInference).toBeDefined();
  });

  it("不改传入的 subset / rules 对象", () => {
    const subset: ColumnSubset = { PORDER: ["POHNUM_0"] };
    const original = rules(true);
    toPreviewRequest(original, ["PORDER"], subset, INDEX);
    expect(subset).toEqual({ PORDER: ["POHNUM_0"] });
    expect(original.joinInference?.inferNameConvention).toBe(true);
  });
});

describe("joinKey / validJoins", () => {
  const j1 = join("PORDERQ", ["POHNUM_0"], "PORDER", ["POHNUM_0"]);
  const j2 = join("PORDER", ["BPRNUM_0"], "BPARTNER", ["BPRNUM_0"]);

  it("joinKey 稳定且区分方向/列序", () => {
    expect(joinKey(j1)).toBe("PORDERQ|POHNUM_0|->|PORDER|POHNUM_0");
    expect(joinKey(j2)).not.toBe(joinKey(j1));
  });

  it("validJoins：两端都被选中的类才返回", () => {
    const all = [j1, j2];
    expect(validJoins(all, new Set(["PORDERQ", "PORDER"]))).toEqual([j1]);
    expect(validJoins(all, new Set(["PORDERQ", "PORDER", "BPARTNER"]))).toEqual(all);
    expect(validJoins(all, new Set())).toEqual([]);
  });
});

describe("buildExecuteRequest", () => {
  it("只提交勾选的类与启用的 join，置 isSelected=true", () => {
    const j1 = join("PORDERQ", ["POHNUM_0"], "PORDER", ["POHNUM_0"]);
    const j2 = join("PORDERQ", ["LIN_0"], "ITMMASTER", ["ITMREF_0"]);
    const preview = previewResponse([j1, j2]);
    const enabled = new Set([joinKey(j1)]);
    const request = buildExecuteRequest(
      preview,
      new Set(["PORDER", "PORDERQ"]),
      enabled,
    );
    expect(request.confirmedClasses.map((c) => c.sourceTable)).toEqual([
      "PORDER",
      "PORDERQ",
    ]);
    expect(request.confirmedClasses.every((c) => c.isSelected)).toBe(true);
    expect(request.confirmedJoins).toEqual([{ ...j1, isSelected: true }]);
    expect(request.syncEmbeddings).toBe(true);
    expect(request.conflictResolutions).toEqual([]);
  });
});
