/** KPI Catalog 类型定义 — 对应后端 Pydantic Schema（Phase 4.1） */

export type KpiStatus = "DRAFT" | "PUBLISHED" | "DEPRECATED";

export const KPI_STATUS_OPTIONS: { value: KpiStatus; labelKey: KpiStatus }[] = [
  { value: "DRAFT", labelKey: "DRAFT" },
  { value: "PUBLISHED", labelKey: "PUBLISHED" },
  { value: "DEPRECATED", labelKey: "DEPRECATED" },
];

export interface KpiCatalog {
  id: number;
  kpiCode: string;
  kpiName: string;
  businessDefinition: string | null;
  formula: string | null;
  numerator: string | null;
  denominator: string | null;
  grain: string | null;
  unit: string | null;
  dataSource: string | null;
  datasourceId: number | null;
  owner: string | null;
  version: string;
  revisionCount: number;
  status: KpiStatus;
  metricId: number | null;
  /** L1 语义匹配关键词。 */
  semanticKeywords: string[] | null;
  /** 语义匹配阈值（0~1，后端默认 0.75）。 */
  matchThreshold: number;
  /** 结构化粒度键。 */
  grainKeys: string[] | null;
  /** 分子列名数组（治理侧声明，对账用）。 */
  numeratorColumns: string[] | null;
  /** 分母列名数组（治理侧声明，对账用）。 */
  denominatorColumns: string[] | null;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

/** P1a：数据源绑定建议（只读提示，需用户手动采用）。 */
export type KpiDatasourceSuggestionReason =
  | "legacy_name_exact"
  | "legacy_name_fuzzy"
  | "legacy_table_exact"
  | "ontology_table";

export interface KpiDatasourceSuggestion {
  datasourceId: number;
  name: string;
  score: number;
  reasons: KpiDatasourceSuggestionReason[];
}

export interface KpiCatalogCreate {
  kpiCode: string;
  kpiName: string;
  datasourceId: number;
  businessDefinition?: string;
  formula?: string;
  numerator?: string;
  denominator?: string;
  grain?: string;
  unit?: string;
  dataSource?: string;
  owner?: string;
  version?: string;
  status?: KpiStatus;
  metricId?: number | null;
  semanticKeywords?: string[];
  matchThreshold?: number;
  grainKeys?: string[];
  numeratorColumns?: string[];
  denominatorColumns?: string[];
  createdBy?: string;
}

export interface KpiCatalogUpdate {
  kpiCode?: string;
  kpiName?: string;
  datasourceId?: number | null;
  businessDefinition?: string | null;
  formula?: string | null;
  numerator?: string | null;
  denominator?: string | null;
  grain?: string | null;
  unit?: string | null;
  dataSource?: string | null;
  owner?: string | null;
  version?: string;
  status?: KpiStatus;
  metricId?: number | null;
  semanticKeywords?: string[] | null;
  matchThreshold?: number | null;
  grainKeys?: string[] | null;
  numeratorColumns?: string[] | null;
  denominatorColumns?: string[] | null;
}
