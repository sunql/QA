/** batchEvalHistory 工具单测（feat-eval-batch-result，2026-09-15）。
 *
 * 覆盖：
 * - 空 storage → list 空 / count 0
 * - appendBatchEvalHistory 推入 → list 拿到新条目
 * - getBatchEvalById 命中 / miss
 * - 超过 MAX_RECORDS（20）自动截断（FIFO，头删尾留）
 * - 损坏 JSON / 非数组数据 → 静默返回空
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { EvaluateBatchResponse } from "../types/dataQualityScore";
import {
  appendBatchEvalHistory,
  buildBatchEvalRecord,
  clearBatchEvalHistory,
  countBatchEvalHistory,
  getBatchEvalById,
  listBatchEvalHistory,
  MAX_RECORDS,
} from "../utils/batchEvalHistory";

function _mkResult(ruleId: number, status: "PASS" | "FAIL" | "ERROR" = "PASS") {
  return {
    ruleId,
    ruleCode: `R${ruleId}`,
    ruleType: "COMPLETENESS" as const,
    datasourceId: 1,
    totalCount: 10,
    passedCount: status === "PASS" ? 10 : status === "FAIL" ? 2 : 0,
    passRate: status === "PASS" ? 100 : status === "FAIL" ? 20 : 0,
    status,
    evaluatedAt: "2026-09-15T00:00:00Z",
    durationMs: 100,
    message: status === "FAIL" ? "未通过：实际通过率 20.00% < 阈值 50.00%" : null,
  };
}

function _mkResponse(
  total: number,
  passed: number,
  failed = 0,
  errored = 0,
): EvaluateBatchResponse {
  const results = [];
  for (let i = 0; i < passed; i++) results.push(_mkResult(i + 1, "PASS"));
  for (let i = 0; i < failed; i++) {
    results.push(_mkResult(passed + i + 1, "FAIL"));
  }
  for (let i = 0; i < errored; i++) {
    results.push(_mkResult(passed + failed + i + 1, "ERROR"));
  }
  return {
    results,
    summaryTotal: total,
    summaryPassed: passed,
  };
}

describe("batchEvalHistory utils", () => {
  beforeEach(() => {
    clearBatchEvalHistory();
  });

  afterEach(() => {
    clearBatchEvalHistory();
  });

  it("returns empty when storage has nothing", () => {
    expect(countBatchEvalHistory()).toBe(0);
    expect(listBatchEvalHistory()).toEqual([]);
    expect(getBatchEvalById("missing")).toBeNull();
  });

  it("appendBatchEvalHistory adds a record and count/list reflects it", () => {
    const resp = _mkResponse(3, 2, 1);
    const saved = appendBatchEvalHistory(resp);
    expect(saved).not.toBeNull();
    expect(saved?.summary.total).toBe(3);
    expect(saved?.summary.passed).toBe(2);
    expect(saved?.summary.failed).toBe(1);
    expect(saved?.summary.errored).toBe(0);
    expect(countBatchEvalHistory()).toBe(1);
    const listed = listBatchEvalHistory();
    expect(listed).toHaveLength(1);
    expect(listed[0].id).toBe(saved?.id);
  });

  it("getBatchEvalById hits / misses", () => {
    const saved = appendBatchEvalHistory(_mkResponse(1, 1));
    expect(getBatchEvalById(saved!.id)?.results[0].ruleCode).toBe("R1");
    expect(getBatchEvalById("nope")).toBeNull();
  });

  it("listBatchEvalHistory returns newest first", () => {
    const r1 = appendBatchEvalHistory(_mkResponse(1, 1));
    // 时间戳精度保证 r2 > r1；用 sleep 兜底避免同毫秒
    const r2 = appendBatchEvalHistory(_mkResponse(1, 1));
    const list = listBatchEvalHistory();
    expect(list[0].id).toBe(r2?.id);
    expect(list[1].id).toBe(r1?.id);
  });

  it("caps at MAX_RECORDS (FIFO, oldest dropped)", () => {
    // 推 25 条 → 只保留 20 条最早的被砍
    for (let i = 0; i < 25; i++) {
      appendBatchEvalHistory(_mkResponse(1, 1));
    }
    expect(countBatchEvalHistory()).toBe(MAX_RECORDS);
    const list = listBatchEvalHistory();
    expect(list).toHaveLength(MAX_RECORDS);
  });

  it("survives corrupt JSON in storage", () => {
    window.localStorage.setItem("qa.dq.batchEvalHistory", "not json {{{");
    expect(listBatchEvalHistory()).toEqual([]);
    expect(countBatchEvalHistory()).toBe(0);
    // append 后能恢复正常
    appendBatchEvalHistory(_mkResponse(1, 1));
    expect(countBatchEvalHistory()).toBe(1);
  });

  it("survives non-array JSON in storage", () => {
    window.localStorage.setItem("qa.dq.batchEvalHistory", JSON.stringify({ a: 1 }));
    expect(listBatchEvalHistory()).toEqual([]);
  });

  it("buildBatchEvalRecord derives summary correctly", () => {
    const resp = _mkResponse(10, 6, 3, 1);
    const record = buildBatchEvalRecord(resp);
    expect(record.summary).toEqual({ total: 10, passed: 6, failed: 3, errored: 1 });
    expect(record.results).toHaveLength(10);
    expect(record.evaluatedAt).toBe("2026-09-15T00:00:00Z");
  });

  it("buildBatchEvalRecord uses now() when results empty", () => {
    const resp: EvaluateBatchResponse = {
      results: [],
      summaryTotal: 0,
      summaryPassed: 0,
    };
    const record = buildBatchEvalRecord(resp);
    expect(record.evaluatedAt).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });
});
