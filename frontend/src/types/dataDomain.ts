// 数据域类型（feat-data-domain-layer 方案 1）
// snake_case DB fields are camelCased by backend CamelModel alias_generator

/** 数据域 Read（GET /api/v1/data-domains）。 */
export interface DataDomain {
  code: string;
  name: string;
  primaryOrgId?: number | null;
  primaryOrgName?: string | null;
  description?: string | null;
  enabled: boolean;
  createdBy?: string | null;
  createdTime: string;
  updatedTime: string;
}

/** 创建载荷：code 走 BusinessObjectCodeNew 同款 format-only 校验（大写/数字/下划线 ≤20）。 */
export interface DataDomainCreate {
  code: string;
  name: string;
  primaryOrgId?: number | null;
  description?: string | null;
  enabled?: boolean;
}

export interface DataDomainUpdate {
  name?: string;
  primaryOrgId?: number | null;
  description?: string | null;
  enabled?: boolean;
}
