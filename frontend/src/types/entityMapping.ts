/** 跨系统编码映射 DTO 类型契约（Phase 3.1 + Phase 4.5 ACL）。
 *
 * 与后端 `EntityMappingCreate / EntityMappingUpdate / EntityMappingRead`
 * 1:1 对齐（camelCase）。
 *
 * Phase 4.5：owner **不再由前端传入**。后端在 create 时从 actor.departments[0]
 * 派生，Update DTO 已移除该字段（防 mass-assignment 越权转移 owner）。前端
 * 仅在 Read DTO 中按响应展示 owner（用于列表展示该实体由哪个部门治理）。
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
  owner: string | null; // 服务端按 actor.departments[0] 派生，前端只读
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
  // owner 故意不暴露给创建表单：服务端从登录用户部门派生
}

export interface EntityMappingUpdate {
  enterpriseCode?: string;
  sourceKey?: string;
  sourceCode?: string;
  matchRule?: MatchRule;
  effectiveDate?: string | null;
  expiryDate?: string | null;
  // owner 故意不暴露给更新表单：owner 变更需走独立特权接口（未实现）
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
