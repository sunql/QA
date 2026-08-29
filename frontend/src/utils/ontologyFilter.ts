/** 本体管理页多条件筛选的纯函数（不依赖 React / DOM，可直接单测）。
 *
 * 约定：
 * - 所有筛选值统一为字符串（Select 选中项由组件转成 String(id)）。
 * - 多条件之间是 AND；值为空串（未输入 / 已清空）的条件被忽略。
 * - 文本类字段子串匹配（大小写不敏感）；下拉类字段精确匹配。
 */
import type {
  OntologyClass,
  OntologyProperty,
  OntologyMetric,
  OntologyJoin,
} from "../types/ontology";

export type FilterValues = Record<string, string>;

/** keyword 为空 → 不过滤；否则大小写不敏感子串匹配（null/undefined 按空串处理）。 */
export function contains(value: unknown, keyword: string): boolean {
  if (keyword === "") return true;
  return String(value ?? "")
    .toLowerCase()
    .includes(keyword.toLowerCase());
}

/** filterValue 为空 → 不过滤；否则精确相等。 */
export function matchSelect(filterValue: string, actual: string): boolean {
  if (filterValue === "") return true;
  return filterValue === actual;
}

/** keyword 为空 → 不过滤；否则任一元素命中子串即通过。 */
export function matchesAny(
  items: string[] | null | undefined,
  keyword: string
): boolean {
  if (keyword === "") return true;
  if (!items) return false;
  return items.some((item) => contains(item, keyword));
}

export function filterClasses(
  rows: OntologyClass[],
  f: FilterValues
): OntologyClass[] {
  return rows.filter(
    (row) =>
      contains(row.className, f.className ?? "") &&
      contains(row.classAlias, f.classAlias ?? "") &&
      contains(row.sourceTable, f.sourceTable ?? "") &&
      contains(row.description, f.description ?? "")
  );
}

export function filterProperties(
  rows: OntologyProperty[],
  f: FilterValues
): OntologyProperty[] {
  return rows.filter(
    (row) =>
      contains(row.propertyName, f.propertyName ?? "") &&
      contains(row.propertyAlias, f.propertyAlias ?? "") &&
      contains(row.sourceColumn, f.sourceColumn ?? "") &&
      matchSelect(f.classId ?? "", String(row.classId)) &&
      matchSelect(f.dataType ?? "", row.dataType)
  );
}

export function filterMetrics(
  rows: OntologyMetric[],
  f: FilterValues
): OntologyMetric[] {
  return rows.filter(
    (row) =>
      contains(row.metricName, f.metricName ?? "") &&
      contains(row.metricAlias, f.metricAlias ?? "") &&
      matchSelect(f.targetClassId ?? "", String(row.targetClassId ?? ""))
  );
}

export function filterJoins(
  rows: OntologyJoin[],
  f: FilterValues
): OntologyJoin[] {
  return rows.filter(
    (row) =>
      matchSelect(f.sourceClassId ?? "", String(row.sourceClassId)) &&
      matchSelect(f.targetClassId ?? "", String(row.targetClassId)) &&
      matchesAny(row.sourceColumns, f.sourceColumns ?? "") &&
      matchesAny(row.targetColumns, f.targetColumns ?? "")
  );
}
