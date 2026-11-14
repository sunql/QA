import { httpClient } from "./client";
import type { KpiGovernanceSummary } from "../types/kpiGovernance";

/** KPI 治理看板聚合（admin-only，只读；非 admin 后端 403）。 */
export async function getKpiGovernanceSummary(): Promise<KpiGovernanceSummary> {
  const res = await httpClient.get<KpiGovernanceSummary>(
    "/admin/kpi/governance-summary",
  );
  return res.data;
}
