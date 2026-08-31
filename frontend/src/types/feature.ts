/** AI Feature Layer 类型契约（Phase 4.3）。
 *
 * 与后端 `FeatureDefinitionCreate / Update / Read / FeatureValueRead /
 * FeatureComputeResult / FeatureComputeBatchResult` 1:1 对齐（camelCase）。
 *
 * owner **不再由前端传入**：后端 create 时从 actor.departments[0] 派生，
 * Update DTO 已移除该字段（防 mass-assignment 越权转移 owner）。前端仅在
 * Read 中按响应展示 owner。
 */

import type { EntityType } from "./entityMapping";

export type FeatureStatus = "DRAFT" | "ACTIVE" | "DEPRECATED";

export type FeatureRefreshFrequency = "DAILY" | "WEEKLY" | "MONTHLY";

export const FEATURE_STATUS_OPTIONS: {
  value: FeatureStatus;
  labelKey: FeatureStatus;
}[] = [
  { value: "DRAFT", labelKey: "DRAFT" },
  { value: "ACTIVE", labelKey: "ACTIVE" },
  { value: "DEPRECATED", labelKey: "DEPRECATED" },
];

export const FEATURE_REFRESH_OPTIONS: {
  value: FeatureRefreshFrequency;
  labelKey: FeatureRefreshFrequency;
}[] = [
  { value: "DAILY", labelKey: "DAILY" },
  { value: "WEEKLY", labelKey: "WEEKLY" },
  { value: "MONTHLY", labelKey: "MONTHLY" },
];

export const FEATURE_ENTITY_TYPES: EntityType[] = [
  "SUPPLIER",
  "MATERIAL",
  "PO",
  "GR",
  "IQC",
  "NCR",
];

export interface FeatureDefinition {
  id: number;
  featureName: string;
  featureAlias: string | null;
  featureDefinition: string | null;
  entityType: EntityType;
  calculationLogic: string;
  windowSize: string | null;
  refreshFrequency: FeatureRefreshFrequency;
  unit: string | null;
  owner: string | null;
  version: string;
  status: FeatureStatus;
  isEnabled: boolean;
  datasourceId: number;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface FeatureDefinitionCreate {
  featureName: string;
  featureAlias?: string;
  featureDefinition?: string;
  entityType: EntityType;
  calculationLogic: string;
  windowSize?: string;
  refreshFrequency?: FeatureRefreshFrequency;
  unit?: string;
  version?: string;
  status?: FeatureStatus;
  isEnabled?: boolean;
  datasourceId: number;
  // owner 故意不暴露给创建表单：服务端从登录用户部门派生
}

export interface FeatureDefinitionUpdate {
  featureName?: string;
  featureAlias?: string | null;
  featureDefinition?: string | null;
  entityType?: EntityType;
  calculationLogic?: string;
  windowSize?: string | null;
  refreshFrequency?: FeatureRefreshFrequency;
  unit?: string | null;
  version?: string;
  status?: FeatureStatus;
  isEnabled?: boolean;
  datasourceId?: number;
  // owner 故意不暴露给更新表单：owner 变更需走独立特权接口（未实现）
}

export interface FeatureValue {
  id: number;
  featureId: number;
  entityKey: string;
  value: string | null; // 后端 Decimal → 字符串（mode=json）
  valueText: string | null;
  validAt: string; // YYYY-MM-DD
  computedAt: string;
}

export interface FeatureComputeResult {
  featureId: number;
  rows: number;
}

export interface FeatureComputeBatchResult {
  results: FeatureComputeResult[];
  totalRows: number;
}
