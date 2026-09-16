/** CSV / TSV 解析器（feat-entity-mapping-bulk-import 2026-09-16）。
 *
 * 设计与选型：
 *  - 自实现最小子集，**不**引第三方 csv 库；现有项目 vitest 已配置但避免新依赖
 *  - 支持双引号包裹 + 双引号转义（RFC 4180 子集）；不支持跨行字段（CSV 标准里是
 *    允许的，本项目模板保证每行一条记录，不需要）
 *  - 自动识别分隔符：尝试 Tab 与逗号，取「解析后行数最多且字段列数稳定」的方案
 *  - 第一行视为表头；行号 1-based（表头 = row 1）
 *
 * 限制（已知）：
 *  - 不支持 Excel 默认导出（CRLF 行尾）之外的 quirks（如 BOM 已 strip）
 *  - 不支持字段内换行（用户在 Excel 里 Alt+Enter 那种）
 *  - 列数与表头不匹配的行被记为「解析失败」，不阻塞其它行
 */

export interface ParseResult {
  /** 自动识别出的分隔符："," 或 "\t" */
  delimiter: "," | "\t";
  /** 表头列名数组（trim 后） */
  headers: string[];
  /** 数据行数组，每行是与 headers 等长的字符串数组 */
  rows: string[][];
  /** 解析过程中遇到的问题（列数不符、空行等），不影响返回 */
  warnings: string[];
}

/** 去掉 UTF-8 BOM（Excel 导出 CSV 默认带）。 */
function stripBom(text: string): string {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

/** 解析一行；支持双引号包裹 + 内部双引号转义（"" → "）。
 *  - 输入假定不含跨行字段
 *  - 末尾空字段保留
 */
export function parseLine(line: string, delimiter: string): string[] {
  const fields: string[] = [];
  let cur = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          // 转义双引号
          cur += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        cur += ch;
      }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (ch === delimiter) {
        fields.push(cur);
        cur = "";
      } else {
        cur += ch;
      }
    }
  }
  fields.push(cur);
  return fields;
}

/** 解析一段文本为 CSV/TSV；自动识别分隔符。 */
export function parseDelimited(text: string): ParseResult {
  const cleaned = stripBom(text);
  // Windows / Unix 都兼容
  const rawLines = cleaned.split(/\r?\n/).filter((line, idx, arr) => {
    // 去掉最后一行空（用户文末多回一次 Enter）
    if (idx === arr.length - 1 && line.trim() === "") return false;
    return true;
  });

  if (rawLines.length === 0) {
    return { delimiter: ",", headers: [], rows: [], warnings: ["empty input"] };
  }

  // 候选分隔符：先 Tab，后逗号；选「解析后所有行都有相同字段数」且行数非零的
  const candidates: ("," | "\t")[] = [",", "\t"];
  let best: { delimiter: "," | "\t"; cols: number; validLines: number } | null = null;
  for (const delim of candidates) {
    const parsed = rawLines.map((l) => parseLine(l, delim));
    if (parsed.length === 0) continue;
    const cols = parsed[0].length;
    if (cols < 2) continue; // 单列 → 不是 CSV/TSV 模板，跳过
    const validLines = parsed.filter((row) => row.length === cols).length;
    if (!best || validLines > best.validLines) {
      best = { delimiter: delim, cols, validLines };
    }
  }

  if (!best) {
    // 退而求其次：强制用 Tab 解析，把第一行当表头
    const fallback = rawLines.map((l) => parseLine(l, "\t"));
    const headers = (fallback[0] || []).map((h) => h.trim());
    return {
      delimiter: "\t",
      headers,
      rows: fallback.slice(1),
      warnings: ["无法自动识别分隔符，已按 Tab 解析（结果可能不准）"],
    };
  }

  const parsed = rawLines.map((l) => parseLine(l, best.delimiter));
  const headers = parsed[0].map((h) => h.trim());
  const expectedCols = parsed[0].length;
  const warnings: string[] = [];
  const rows: string[][] = [];
  for (let i = 1; i < parsed.length; i++) {
    const row = parsed[i];
    if (row.length === expectedCols) {
      rows.push(row.map((c) => c.trim()));
    } else {
      warnings.push(
        `row ${i + 1} 列数不符（期望 ${expectedCols} 列，实际 ${row.length} 列）：${rawLines[i].slice(0, 80)}`,
      );
    }
  }
  return { delimiter: best.delimiter, headers, rows, warnings };
}

/** 按列名取字段值（headers + rows → key-value 数组）。 */
export interface RowRecord {
  row: number; // 1-based（含表头），数据行从 2 开始
  values: Record<string, string>;
}

export function rowsToRecords(
  parsed: ParseResult,
): RowRecord[] {
  return parsed.rows.map((row, idx) => {
    const record: Record<string, string> = {};
    parsed.headers.forEach((h, i) => {
      record[h] = row[i] ?? "";
    });
    return { row: idx + 2, values: record };
  });
}