/** 表格/图表导出工具（纯前端，零依赖）。
 *
 * - toCsv：纯函数，行数据 → UTF-8 BOM CSV 文本（便于单测）。
 * - downloadBlob / downloadCsv：对象 URL + 动态 <a download> 触发下载。
 */

const BOM = "﻿";

/** 单元格转义：null/undefined → 空串；含 `,"` 或换行的字段用双引号包裹并双写内部引号。 */
function escapeCsvCell(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  const s = String(value);
  if (/[",\r\n]/.test(s)) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

/** 行数据 → CSV 文本。表头取首行 key（与 ChartRenderer 表格列派生一致）。
 * 空数组返回 ""（无 BOM）；非空前缀 BOM（Excel 直开不乱码），行分隔 CRLF。
 */
export function toCsv(rows: Record<string, unknown>[]): string {
  if (rows.length === 0) {
    return "";
  }
  const headers = Object.keys(rows[0]);
  const lines = [headers.map(escapeCsvCell).join(",")];
  for (const row of rows) {
    lines.push(headers.map((header) => escapeCsvCell(row[header])).join(","));
  }
  return `${BOM}${lines.join("\r\n")}`;
}

/** 触发浏览器下载：对象 URL → 动态 <a download> → 点击后延迟回收 URL。
 * 延迟 1s 回收，避免点击后立即 revoke 中断下载。
 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 行数据 → CSV 文件下载。 */
export function downloadCsv(rows: Record<string, unknown>[], filename: string): void {
  downloadBlob(new Blob([toCsv(rows)], { type: "text/csv;charset=utf-8" }), filename);
}
