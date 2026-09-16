/** CSV 下载工具（feat-entity-mapping-bulk-import 2026-09-16）。
 *
 * 抽出来便于单测；UI 组件直接调 downloadCsv。
 *
 * 行为：
 *  - 前置 UTF-8 BOM（Excel 中文打开防乱码）
 *  - 用 Blob + ObjectURL + a[download] 触发下载
 *  - revokeObjectURL 清理 URL，避免内存泄漏
 *  - 浏览器专用（依赖 document / URL.createObjectURL）
 */

/** 触发浏览器下载一段 CSV 文本。 */
export function downloadCsv(
  csv: string,
  filename: string,
  options: { withBom?: boolean } = {},
): void {
  const { withBom = true } = options;
  const BOM = "﻿";
  const content = withBom ? BOM + csv : csv;
  const blob = new Blob([content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/** 把 File 异步读为 UTF-8 字符串；带大小校验与后缀校验。 */
export function readFileAsText(
  file: File,
  opts: {
    /** 文件大小上限（字节）；默认 1MB */
    maxBytes?: number;
    /** 允许的文件后缀（小写，含点）；默认 .csv / .tsv / .txt */
    allowedExts?: string[];
  } = {},
): Promise<string> {
  const maxBytes = opts.maxBytes ?? 1024 * 1024;
  const allowedExts = opts.allowedExts ?? [".csv", ".tsv", ".txt"];
  const lower = file.name.toLowerCase();
  if (!allowedExts.some((ext) => lower.endsWith(ext))) {
    return Promise.reject(
      new Error(`不支持的文件后缀：${file.name}（仅 ${allowedExts.join("/")}）`),
    );
  }
  if (file.size > maxBytes) {
    return Promise.reject(
      new Error(
        `文件过大：${(file.size / 1024).toFixed(1)} KB > ${(maxBytes / 1024).toFixed(0)} KB`,
      ),
    );
  }
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("文件读取失败"));
    reader.readAsText(file, "utf-8");
  });
}