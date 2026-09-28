/** Routing metrics snapshot types (Task 5.3 — feat-complex-metric-pipeline).
 *
 * Backend contract: RoutingMetricsService(session).get_snapshot(since, until)
 * returns RoutingMetricsSnapshot via GET /api/v1/routing-metrics/snapshot (not yet implemented).
 *
 * 3-layer routing (2026-09-27 update):
 * - L1: KpiSemanticMatchService Jaccard similarity match
 * - L2: LLM single-step NL2SQL（含多步拆解 `_executeMultiStep`，同样记 routing_layer="L2"）
 * - L4: Agent Loop（纯 Python async while loop，非 LangGraph）
 *
 * 注：旧"L3 多步链式推理"已于 2026-09-27 删除（M5 批）；多步由 L2 承担。
 * 前端联合类型已移除 L3。
 */

/** Per-layer metric atom. */
export interface LayerMetric {
  layer: "L1" | "L2" | "L4";
  hitCount: number;
  avgDurationMs: number;
  avgTokenCost: number;
}

/** Full snapshot envelope returned by the backend endpoint. */
export interface RoutingMetricsSnapshot {
  since: string;   // ISO-8601 datetime
  until: string;   // ISO-8601 datetime
  layerDistribution: LayerMetric[];
  totalQueries: number;
  avgTotalDurationMs: number;
}