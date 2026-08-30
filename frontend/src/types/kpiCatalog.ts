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
  owner: string | null;
  version: string;
  revisionCount: number;
  status: KpiStatus;
  metricId: number | null;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface KpiCatalogCreate {
  kpiCode: string;
  kpiName: string;
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
  createdBy?: string;
}

export interface KpiCatalogUpdate {
  kpiCode?: string;
  kpiName?: string;
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
}
