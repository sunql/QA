// Supplier Risk Agent（Phase 5.4）
// 对齐后端 SupplierRiskKpiContribution / SupplierRiskRead / RiskLevel。
// 主路径：RISK_SCORE (0-1) → High/Medium/Low；Fallback：3 个 feature 违规计数。

import type { Supplier360Profile } from "./supplier";

export type RiskLevel = "high" | "medium" | "low" | "unknown";

export interface SupplierRiskKpiContribution {
  featureName: string;
  featureAlias: string | null;
  /** Decimal → string（与 Supplier360Kpi 一致）。 */
  value: string | null;
  unit: string | null;
  threshold: string | null;
  /** True=未触发违规，False=触发。 */
  passed: boolean;
  note: string | null;
}

export interface SupplierRiskRead {
  /** 复用 Supplier360Profile（profile 来源不变）。 */
  profile: Supplier360Profile;
  level: RiskLevel;
  /** "risk_score" / "fallback_composite" / "unknown"。 */
  levelSource: string;
  contributions: SupplierRiskKpiContribution[];
  /** LLM 生成的 1-2 句中文风险描述；fallback 时为模板字符串。 */
  riskPoints: string | null;
  /** "llm" / "fallback_template"。 */
  riskPointsSource: string;
  recommendedActions: string[];
  tokensUsed: number;
  cost: number;
  llmModelName: string | null;
  fetchedAt: string;
}