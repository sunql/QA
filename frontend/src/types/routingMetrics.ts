/** Routing metrics snapshot types (Task 5.3 — feat-complex-metric-pipeline).
 *
 * Backend contract: RoutingMetricsService(session).get_snapshot(since, until)
 * returns RoutingMetricsSnapshot via GET /api/v1/routing-metrics/snapshot (not yet implemented).
 *
 * 4-layer routing:
 * - L1: KpiSemanticMatchService Jaccard similarity match
 * - L2: LLM CTE intent classification
 * - L3: ChainedStep CTE multi-step chain
 * - L4: LangGraph Agent Loop
 */

/** Per-layer metric atom. */
export interface LayerMetric {
  layer: "L1" | "L2" | "L3" | "L4";
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
