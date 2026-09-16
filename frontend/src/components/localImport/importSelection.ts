// 本地导入向导的纯选择逻辑：表/列白名单 + 关联推断开关 → 预览请求、执行请求。
// 全部为不可变纯函数（不改入参），便于 vitest 覆盖；组件只消费返回值。

import type { TableSchema } from "../../types/datasource";
import type {
  ImportExecuteRequest,
  ImportPreviewRequest,
  ImportPreviewResponse,
  ImportRuleConfig,
  ProposedJoin,
} from "../../types/localImport";

export interface JoinInferenceFlags {
  inferDeclaredFk: boolean;
  inferNameConvention: boolean;
}

export const DEFAULT_JOIN_INFERENCE: JoinInferenceFlags = {
  inferDeclaredFk: true,
  inferNameConvention: true,
};

// 列选白名单：{表名: 选中列}，仅记录「被收窄到真子集」的表；省略 = 该表全列。
export type ColumnSubset = Record<string, string[]>;

// 表名查找表：避免多次线性扫；不改 tables。
export function tableNameIndex(tables: readonly TableSchema[]): Map<string, TableSchema> {
  const index = new Map<string, TableSchema>();
  for (const table of tables) {
    index.set(table.tableName, table);
  }
  return index;
}

// 表的全部列名（保持 schema 顺序）。
export function allColumnNames(table: TableSchema | undefined): string[] {
  return table ? table.columns.map((c) => c.columnName) : [];
}

// 单表当前勾选列：subset 有该表条目时取之（可能等于全列），否则视为全列。
export function chosenColumns(
  subset: ColumnSubset,
  tableName: string,
  full: readonly string[]
): string[] {
  const chosen = subset[tableName];
  return chosen && chosen.length > 0 ? chosen : [...full];
}

// 把 joinInference 开关合入 rules（返回新对象，不改入参）。
export function toRulesWithJoinInference(
  rules: ImportRuleConfig,
  flags: JoinInferenceFlags
): ImportRuleConfig {
  return {
    ...rules,
    joinInference: {
      inferDeclaredFk: flags.inferDeclaredFk,
      inferNameConvention: flags.inferNameConvention,
    },
  };
}

function isStrictSubset(chosen: string[], full: string[]): boolean {
  if (chosen.length === 0) {
    return false; // 空选 = 不导入任何列，让后端忽略此表列选即全列 —— 用「取消选表」表达，故不视为收窄
  }
  if (chosen.length >= full.length) {
    return false; // 选了全部列 → 等价全列，无需下发
  }
  const fullSet = new Set(full);
  return chosen.every((c) => fullSet.has(c));
}

// 收窄 subset：丢弃未选表的条目；选中表若子集已达全列则移除该条目（改回全列）。
// 返回新对象，不改输入。
export function pruneColumnSubset(
  subset: ColumnSubset,
  selectedTables: readonly string[],
  index: Map<string, TableSchema>
): ColumnSubset {
  const next: ColumnSubset = {};
  for (const table of selectedTables) {
    const chosen = subset[table];
    const full = allColumnNames(index.get(table));
    if (chosen && isStrictSubset(chosen, full)) {
      next[table] = [...chosen];
    }
  }
  return next;
}

// 组预览请求。未选任何表时返回 selectedTables=undefined（后端语义为预览全部表，
// 调用方通常用「未选表时禁用按钮」拦下，避免超大 schema 全量预览）。
// selectedColumns 只含被收窄的表（真子集），其余表保持全列。
export function toPreviewRequest(
  rules: ImportRuleConfig,
  selectedTables: readonly string[],
  subset: ColumnSubset,
  index: Map<string, TableSchema>
): ImportPreviewRequest {
  const normalized = pruneColumnSubset(subset, selectedTables, index);
  return {
    rules,
    selectedTables: selectedTables.length > 0 ? [...selectedTables] : undefined,
    selectedColumns: Object.keys(normalized).length > 0 ? normalized : undefined,
  };
}

// join 的唯一键（行选择 / 开关去重用）。列数相等、同列序才视为同一条边。
export function joinKey(j: ProposedJoin): string {
  return [
    j.sourceTable,
    j.sourceColumns.join(","),
    "->",
    j.targetTable,
    j.targetColumns.join(","),
  ].join("|");
}

// 预览中两端都被当前类勾选覆盖的 join（可落库候选）。
export function validJoins(
  joins: readonly ProposedJoin[],
  selectedClassKeys: ReadonlySet<string>
): ProposedJoin[] {
  const next: ProposedJoin[] = [];
  for (const j of joins) {
    if (selectedClassKeys.has(j.sourceTable) && selectedClassKeys.has(j.targetTable)) {
      next.push(j);
    }
  }
  return next;
}

// 组装执行请求：只提交勾选的类与保留的 join。
// selectedClassKeys 以 sourceTable 为键；enabledJoinKeys 以 joinKey() 为键。
export function buildExecuteRequest(
  preview: ImportPreviewResponse,
  selectedClassKeys: ReadonlySet<string>,
  enabledJoinKeys: ReadonlySet<string>
): ImportExecuteRequest {
  const confirmedClasses = preview.proposedClasses
    .filter((c) => selectedClassKeys.has(c.sourceTable))
    .map((c) => ({ ...c, isSelected: true }));
  const confirmedJoins = preview.proposedJoins
    .filter(
      (j) =>
        selectedClassKeys.has(j.sourceTable) &&
        selectedClassKeys.has(j.targetTable) &&
        enabledJoinKeys.has(joinKey(j))
    )
    .map((j) => ({ ...j, isSelected: true }));
  return {
    confirmedClasses,
    confirmedJoins,
    conflictResolutions: [],
    // 导入完成后由后端整批补齐类向量（单次 flush），无需人工再点「补同步缺失向量」
    syncEmbeddings: true,
  };
}
