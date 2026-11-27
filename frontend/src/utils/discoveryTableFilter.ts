/** 发现运行的**表选择器**过滤逻辑（feat/discovery-skip-unprofilable-tables）。
 *
 * 两个诉求（用户 2026-11-27）：
 * 1. **别显示那些表** —— Oracle 回收站对象（`BIN$W81vJmasuczgY0bNqMBvog==$0`）
 *    含 `=` ⇒ 不在 SQL 标识符白名单内 ⇒ 剖析护栏必然拒拼 ⇒ 一张就能让整轮运行失败。
 *    这些表由**后端**判定（`domain.sql_identifier.isValidIdentifier`，与护栏同源），
 *    前端只负责不显示它们。
 * 2. **能搜** —— 「排查哪些表 / 找出包含某些列的表」：搜索词命中**表名**、
 *    **属主.表名** 或**列名**都算命中；按列名命中时额外标出是哪一列。
 *
 * 纯函数（无 React 依赖）⇒ 可单测。
 */

/** 选择器的一个选项（`value` = 表名，`label` = 展示用的 `属主.表名`）。 */
export interface DiscoveryTableOption {
  value: string;
  label: string;
  /** 列名列表（用于「按列名搜表」）。 */
  columns: string[];
}

/** 一次搜索的结果。 */
export interface TableMatchResult {
  /** 是否命中（空搜索词 ⇒ 恒命中）。 */
  matched: boolean;
  /** 按**列名**命中时的列名（按表名/属主命中时为 null）。 */
  columnHit: string | null;
}

/**
 * 判断一个选项是否命中搜索词（大小写不敏感的**包含**匹配）。
 *
 * 命中顺序：表名/属主.表名 → 列名。空查询 ⇒ 命中且无列命中。
 */
export function matchTableOption(
  option: DiscoveryTableOption,
  query: string,
): TableMatchResult {
  const needle = query.trim().toUpperCase();
  if (!needle) {
    return { matched: true, columnHit: null };
  }
  if (option.label.toUpperCase().includes(needle)) {
    return { matched: true, columnHit: null };
  }
  const columnHit =
    option.columns.find((column) => column.toUpperCase().includes(needle)) ?? null;
  return { matched: columnHit !== null, columnHit };
}

/**
 * 由快照 + 后端给的「不可剖析表名」构造选择器选项。
 *
 * @param tables 快照里的表（`tableName` / `owner` / `columns`）
 * @param unprofilableTables 后端判定的不可剖析表名（按表名精确匹配，大小写不敏感）
 * @returns 选项列表 + 被隐藏的表名（用于提示「已隐藏 N 张」）
 */
export function buildDiscoveryTableOptions(
  tables: ReadonlyArray<{
    tableName: string;
    owner?: string;
    columns?: ReadonlyArray<{ columnName: string }>;
  }>,
  unprofilableTables: ReadonlyArray<string> = [],
): { options: DiscoveryTableOption[]; hiddenTableNames: string[] } {
  const hidden = new Set(unprofilableTables.map((name) => name.toUpperCase()));
  const options: DiscoveryTableOption[] = [];
  const hiddenTableNames: string[] = [];
  for (const table of tables) {
    const name = table.tableName;
    if (!name) {
      continue;
    }
    if (hidden.has(name.toUpperCase())) {
      hiddenTableNames.push(name);
      continue;
    }
    const owner = table.owner ? `${table.owner}.` : "";
    options.push({
      value: name,
      label: `${owner}${name}`,
      columns: (table.columns ?? []).map((column) => column.columnName),
    });
  }
  return { options, hiddenTableNames };
}
