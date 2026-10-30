import type { ReportBlock } from "../types/chat";

/**
 * 系统边界收窄：后端/历史回放的 reportSections 是原始 JSON，形状可能来自
 * 更早版本。逐项校验并丢弃非法块；没有合法块时返回 null（= 走旧渲染）。
 */
function isReportBlock(value: unknown): value is ReportBlock {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  if (record.kind === "text") {
    return typeof record.content === "string";
  }
  if (record.kind === "chart") {
    return typeof record.stepIndex === "number" && Number.isInteger(record.stepIndex);
  }
  return false;
}

export function asReportSections(value: unknown): ReportBlock[] | null {
  if (!Array.isArray(value)) return null;
  const blocks = value.filter(isReportBlock);
  return blocks.length > 0 ? blocks : null;
}
