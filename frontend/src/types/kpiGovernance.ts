/** KPI 治理看板响应类型。
 *
 * 与后端 `app/domain/schemas.py::KpiGovernanceSummary`（CamelModel）逐字段对齐：
 * 字段名 camelCase，后端显式 alias 的 `neo4jKpiCount` 保持小写 `neo4j`
 * （`to_camel` 会把它拆成 `neo4JKpiCount`，所以后端钉住了 alias）。
 *
 * 形状契约见 docs/superpowers/specs/2026-11-14-kpi-p4b-p5-design.md §2.3。
 */

export interface KpiGovernanceKpiCounts {
  total: number;
  byStatus: Record<string, number>;
}

export interface KpiGovernanceMetricCounts {
  total: number;
  /** NULL / 空串统一归入 `NONE` 桶（与运行时 `execution_source_missing` 同口径）。 */
  byExecutionSource: Record<string, number>;
  byStatus: Record<string, number>;
}

export interface KpiGovernanceReconcile {
  windowDays: number;
  mismatchCount: number;
  /** 窗口内实际落库的 pass 行数；只有 degraded pass 会落库（干净 pass 不落审计），不是通过率。 */
  passCount: number;
  unknownCount: number;
  /** 窗口内最近一条 mismatch 时间；无则 null。 */
  lastMismatchAt: string | null;
  /** 审计查询失败的短错误串；正常为 null。 */
  reconcileError: string | null;
}

export interface KpiGovernanceRuntimeReason {
  reason: string;
  count: number;
}

/** 运行时 Tier 分布（数据源：audit_log 的 `kpi_runtime_event`，按实际服务层计）。 */
export interface KpiGovernanceRuntime {
  windowDays: number;
  /** 窗口内 KPI 路由事件数（A/B/C/LEGACY 各一次）。 */
  total: number;
  /** 实际服务层分布；Tier A 执行失败降级时计入 B。 */
  byTier: Record<string, number>;
  /** 降级原因 Top-5（按次数降序、同频按名称）。 */
  degradedReasons: KpiGovernanceRuntimeReason[];
  chatAnswers: number;
  /** 近似命中率 = total / chatAnswers（事件/回答）；无回答时为 null。 */
  hitRate: number | null;
  runtimeError: string | null;
}

export interface KpiGovernanceSyncHealth {
  /** 外部计数失败时为 null（best-effort，不让看板整体失败）。 */
  publishedKpiCount: number | null;
  milvusKpiCount: number | null;
  /** 后端显式 alias；注意是小写 `neo4j`。 */
  neo4jKpiCount: number | null;
  kpiTotal: number;
  syncHealthError: string | null;
}

export interface KpiGovernanceGates {
  publishGateEnabled: boolean;
  tierAEnabled: boolean;
  syncEnabled: boolean;
}

export interface KpiGovernanceSummary {
  kpis: KpiGovernanceKpiCounts;
  metrics: KpiGovernanceMetricCounts;
  reconcile: KpiGovernanceReconcile;
  runtime: KpiGovernanceRuntime;
  syncHealth: KpiGovernanceSyncHealth;
  gates: KpiGovernanceGates;
  generatedAt: string;
}
