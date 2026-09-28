/** 批量评估结果历史（feat-eval-batch-result，2026-09-15）。
 *
 * localStorage 持久化最近 20 条「批量评估结果」，让用户能回顾之前跑过的批次。
 * - 仅前端保存（用户选择：不上后端）
 * - SSR-safe：`typeof window` 守卫
 * - 头删尾留（FIFO）：超过 MAX_RECORDS 自动截断最早的
 * - 写失败不抛（隐私模式 / quota 超限）：返回 false 让上层降级
 */

import type { EvaluateBatchResponse, EvaluationResult } from "../types/dataQualityScore";

const STORAGE_KEY = "qa.dq.batchEvalHistory";
/** 历史记录上限（导出供测试 / 未来 UI 提示用）。 */
export const MAX_RECORDS = 20;

/** 单条历史记录：纯数据 + 时间戳，方便 Drawer 列表展示。 */
export interface BatchEvalRecord {
  id: string; // UUID-like，简易 ID：Date.now().toString(36) + 随机后缀
  evaluatedAt: string; // ISO string
  summary: {
    total: number;
    passed: number;
    failed: number;
    errored: number;
  };
  /** 取该 batch 里第一条结果的 evaluatedAt 作为「批次时间」 */
  results: EvaluationResult[];
}

/** 读 storage，失败返回空数组。模块级缓存可选——为简单起见每次 list 都现读。 */
function _readStorage(): BatchEvalRecord[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed as BatchEvalRecord[];
  } catch {
    return [];
  }
}

function _writeStorage(records: BatchEvalRecord[]): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
    return true;
  } catch {
    return false;
  }
}

/** 给定 EvaluateBatchResponse → BatchEvalRecord（不含 id / evaluatedAt 顶层，由 caller 补）。 */
export function buildBatchEvalRecord(
  response: EvaluateBatchResponse,
): Omit<BatchEvalRecord, "id"> {
  const failed = response.results.filter((r) => r.status === "FAIL").length;
  const errored = response.results.filter((r) => r.status === "ERROR").length;
  // 取第一条的 evaluatedAt 作为「批次时间」；空 batch 退化到 now
  const evaluatedAt =
    response.results.length > 0 && response.results[0].evaluatedAt
      ? response.results[0].evaluatedAt
      : new Date().toISOString();
  return {
    evaluatedAt,
    summary: {
      total: response.summaryTotal,
      passed: response.summaryPassed,
      failed,
      errored,
    },
    results: response.results,
  };
}

/** 推一条到历史尾部，超过 MAX 自动截断最早的。返回写入的完整 record（含新 id）。 */
export function appendBatchEvalHistory(
  response: EvaluateBatchResponse,
): BatchEvalRecord | null {
  const record: BatchEvalRecord = {
    id:
      Date.now().toString(36) +
      "-" +
      Math.random().toString(36).slice(2, 8),
    ...buildBatchEvalRecord(response),
  };
  const current = _readStorage();
  current.push(record);
  // FIFO：保留最近 MAX_RECORDS 条
  const trimmed = current.slice(-MAX_RECORDS);
  return _writeStorage(trimmed) ? record : null;
}

/** 历史条数（用于 Badge 数字）。 */
export function countBatchEvalHistory(): number {
  return _readStorage().length;
}

/** 历史列表（最新在前）。 */
export function listBatchEvalHistory(): BatchEvalRecord[] {
  return _readStorage().slice().reverse();
}

/** 按 id 取单条；找不到返回 null。 */
export function getBatchEvalById(id: string): BatchEvalRecord | null {
  return _readStorage().find((r) => r.id === id) ?? null;
}

/** 清空全部历史（测试用 / 未来 UI 「清空历史」按钮）。 */
export function clearBatchEvalHistory(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // 静默
  }
}
