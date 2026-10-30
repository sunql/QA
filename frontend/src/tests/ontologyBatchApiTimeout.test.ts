/** 长操作 API per-call 超时守卫（2026-10-23，CentOS7 首装实测踩中）。
 *
 * 背景：axios 全局 REQUEST_TIMEOUT_MS=30s，批量关系引擎（POST /ontology/batch）
 * 在生产数据量下超 30s ⇒ axios 客户端掐断 ⇒ 前端报「网络异常」（status=0 路径）。
 * nginx proxy_read_timeout=300s 是硬上限，per-call 超时必须留余量（280s）。
 *
 * 例外（2026-10-24）：runOntologyBatch 用 300s 临时等待预算（生产批量执行
 * 实测可贴近上限；长期方案是 202+后台任务+轮询，届时移除）。其余 3 个长操作
 * 仍走 LONG_RUNNING_TIMEOUT_MS(280s)。
 *
 * SSOT：config.ts LONG_RUNNING_TIMEOUT_MS；本测试锁定各调用的超时值。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  backfillRelations,
  runOntologyBatch,
  previewOntologyBatch,
  syncMissingGraph,
} from "../api/ontology";
import { LONG_RUNNING_TIMEOUT_MS } from "../config";

describe("ontology 长操作 per-call 超时", () => {
  beforeEach(() => {
    httpMock.post.mockReset();
    httpMock.post.mockResolvedValue({ data: {} });
  });

  it("批量执行 POST /ontology/batch 用 300s 临时等待预算", async () => {
    await runOntologyBatch({ syncGraph: true, onConflict: "skip" });
    expect(httpMock.post).toHaveBeenCalledWith(
      "/ontology/batch",
      { syncGraph: true, onConflict: "skip" },
      { timeout: 300_000 }
    );
  });

  it("批量预览 POST /ontology/batch/preview 带 280s 超时", async () => {
    await previewOntologyBatch({ inferJoins: true, onConflict: "skip" });
    expect(httpMock.post).toHaveBeenCalledWith(
      "/ontology/batch/preview",
      { inferJoins: true, onConflict: "skip" },
      { timeout: LONG_RUNNING_TIMEOUT_MS }
    );
  });

  it("一键补关系 POST /ontology/relations/backfill 带 280s 超时", async () => {
    await backfillRelations();
    expect(httpMock.post).toHaveBeenCalledWith(
      "/ontology/relations/backfill",
      undefined,
      { timeout: LONG_RUNNING_TIMEOUT_MS }
    );
  });

  it("图谱对账 POST /ontology/graph/sync-missing 带 280s 超时", async () => {
    await syncMissingGraph();
    expect(httpMock.post).toHaveBeenCalledWith(
      "/ontology/graph/sync-missing",
      undefined,
      { timeout: LONG_RUNNING_TIMEOUT_MS }
    );
  });

  it("长超时常量必须留余量小于 nginx 300s 上限", () => {
    expect(LONG_RUNNING_TIMEOUT_MS).toBe(280_000);
  });

  it("批量执行的临时预算不得低于常量预算（临时指放宽，不是收紧）", () => {
    expect(300_000).toBeGreaterThanOrEqual(LONG_RUNNING_TIMEOUT_MS);
  });
});
