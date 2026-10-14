/** Task 14：证据面板 summary 结构化部分 ⇒ 只读展示形状（纯函数）。
 *
 * 后端 `EvidenceSummary`（`research_evidence_summary.py`）是 `CamelModel`，线上经
 * `model_dump(by_alias=True)` 序列化 ⇒ 键为 camelCase：
 * - `topN`：`Array<{ rank: number; row: Record<string, unknown> }>`，`row` 是**整行原始列**
 *   （原始列名 → 值），默认 `[]`。
 * - `timeRange`：`{ start: string; end: string } | null`（两个标签，无取值序列）。
 *
 * 后端在内部用 `_metricValues` / `_topNRows` 计算度量列，但**不导出**其身份 ⇒ 客户端
 * **无从**断定 `row` 里哪列是「值」、哪列是「名称」⇒ 本模块只产出**表 / 标签形状**，
 * **不画图、不猜度量列**（真正的 Top-N 柱状图需后端补 `metricColumn`，登记为后续项）。
 *
 * 纯函数：不读 store / 不发请求 / **不改入参**。
 */

export interface TopNTableShape {
  columns: string[];
  rows: Array<Record<string, string>>;
}

/** Top-N 次序列的键名（合成列，非原始行键）。 */
const RANK_COLUMN = "rank";

/** 单元格 → 字符串：null/undefined ⇒ ""；对象/数组 ⇒ JSON（不产出 "[object Object]"）；其余 String。 */
function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** `summary.topN` ⇒ 紧凑表形状（列 = `rank` + 各行键并集，按首次出现顺序）；无有效行 ⇒ `null`。 */
export function topNTableShape(summary: Record<string, unknown> | null): TopNTableShape | null {
  const topN = summary?.topN;
  if (!Array.isArray(topN)) return null;

  const columns: string[] = [RANK_COLUMN];
  const rows: Array<Record<string, string>> = [];
  for (const item of topN) {
    if (typeof item !== "object" || item === null) continue;
    const { rank, row } = item as { rank?: unknown; row?: unknown };
    if (typeof row !== "object" || row === null) continue;

    const cells: Record<string, string> = {};
    for (const [key, value] of Object.entries(row as Record<string, unknown>)) {
      if (!columns.includes(key)) columns.push(key);
      cells[key] = formatCell(value);
    }
    // 合成 rank 列权威（Top-N 次序）；与原始行同名键冲突时以合成值为准（见后续项）。
    rows.push({ ...cells, [RANK_COLUMN]: formatCell(rank) });
  }
  return rows.length > 0 ? { columns, rows } : null;
}

/** `summary.timeRange` ⇒ `{ start, end }`；缺省 / 非法（非对象 / 非字符串标签）⇒ `null`。 */
export function timeRangeLabels(
  summary: Record<string, unknown> | null
): { start: string; end: string } | null {
  const timeRange = summary?.timeRange;
  if (typeof timeRange !== "object" || timeRange === null) return null;
  const { start, end } = timeRange as { start?: unknown; end?: unknown };
  if (typeof start !== "string" || typeof end !== "string") return null;
  return { start, end };
}
