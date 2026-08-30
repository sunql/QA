/** 跨系统编码映射 DTO 类型契约（Phase 3.1）。
 * 与后端 `EntityMappingCreate / EntityMappingUpdate / EntityMappingRead` 1:1 对齐（camelCase）。
 */

export type EntityType = "SUPPLIER" | "MATERIAL" | "PO" | "GR" | "IQC" | "NCR";

export type SourceSystem = "ERP" | "SRM" | "QMS" | "MDM" | "PLM";

export type MatchRule = "MDM_MASTER" | "BUSINESS_KEY" | "MAPPING";

export interface EntityMappingBase {
  entityType: EntityType;
  enterpriseKey: number;
  enterpriseCode: string;
  sourceSystem: SourceSystem;
  sourceKey: string;
  sourceCode: string;
  matchRule: MatchRule;
  effectiveDate: string | null; // YYYY-MM-DD
  expiryDate: string | null; // YYYY-MM-DD，空表示长期有效
}

export interface EntityMappingCreate {
  entityType: EntityType;
  enterpriseKey: number;
  enterpriseCode: string;
  sourceSystem: SourceSystem;
  sourceKey: string;
  sourceCode: string;
  matchRule?: MatchRule;
  effectiveDate?: string | null;
  expiryDate?: string | null;
}

export interface EntityMappingUpdate {
  enterpriseCode?: string;
  sourceKey?: string;
  sourceCode?: string;
  matchRule?: MatchRule;
  effectiveDate?: string | null;
  expiryDate?: string | null;
}

export interface EntityMappingRead extends EntityMappingBase {
  id: number;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface EntityMappingListFilter {
  entityType?: EntityType;
  sourceSystem?: SourceSystem;
  enterpriseKey?: number;
}
