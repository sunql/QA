/** 数据质量规则类型 — 对应后端 DataQualityRuleCreate/Update/Read（camelCase 契约）。 */

export type RuleType =
  | "COMPLETENESS"
  | "VALIDITY"
  | "UNIQUENESS"
  | "CONSISTENCY"
  | "REFERENTIAL"
  | "TIMELINESS";

export type Severity = "HIGH" | "MEDIUM" | "LOW" | "INFO";

export interface DataQualityRule {
  id: number;
  ruleName: string;
  ruleCode: string;
  datasourceId: number;
  targetTable: string;
  targetColumn: string | null;
  ruleType: RuleType;
  ruleExpression: string | null;
  threshold: string; // 后端返回 DECIMAL 序列化为字符串
  severity: Severity;
  isEnabled: boolean;
  version: string;
  owner: string | null;
  description: string | null;
  /** 后端 DataQualityRuleRead.source_class_id，nullable（历史/无对象归属规则） */
  sourceClassId?: number | null;
  sourcePropertyId?: number | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface DataQualityRuleCreate {
  ruleName: string;
  ruleCode: string;
  datasourceId: number;
  targetTable: string;
  targetColumn?: string | null;
  ruleType: RuleType;
  ruleExpression?: string | null;
  threshold?: string;
  severity?: Severity;
  isEnabled?: boolean;
  version?: string;
  owner?: string | null;
  description?: string | null;
}

export interface DataQualityRuleUpdate {
  ruleName?: string;
  datasourceId?: number;
  targetTable?: string;
  targetColumn?: string | null;
  ruleType?: RuleType;
  ruleExpression?: string | null;
  threshold?: string;
  severity?: Severity;
  isEnabled?: boolean;
  version?: string;
  owner?: string | null;
  description?: string | null;
}

export interface DataQualityRuleListParams {
  ruleType?: RuleType;
  targetTable?: string;
  targetTables?: string[];
  enabledOnly?: boolean;
  ruleName?: string;
  datasourceId?: number;
  severity?: Severity;
  enabled?: "all" | "enabled" | "disabled";
  sourceClassId?: number;
}

/** GET /data-quality/rules/options 响应（feat-dq-rule-list-filters）。 */
export interface DatasourceOption {
  id: number;
  name: string;
}

export interface ClassOption {
  id: number;
  className: string;
}

export interface RuleOptions {
  ruleNames: string[];
  datasourceIds: DatasourceOption[];
  targetTables: string[];
  severities: Severity[];
  classOptions: ClassOption[];
}