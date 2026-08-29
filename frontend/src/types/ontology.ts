/** 本体（Ontology）类型定义 — 对应后端 Pydantic Schema */

// ===== Class =====

export interface OntologyClass {
  id: number;
  className: string;
  classAlias: string | null;
  description: string | null;
  sourceTable: string | null;
  parentClassId: number | null;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
  /** 版本管理（Phase 6）：1 起始，每次更新 +1；历史版本 validTo 非空 */
  version: number;
  validFrom: string | null;
  validTo: string | null;
}

export interface OntologyClassCreate {
  className: string;
  classAlias?: string;
  description?: string;
  sourceTable?: string;
  parentClassId?: number;
  createdBy?: string;
}

export interface OntologyClassUpdate {
  className?: string;
  classAlias?: string;
  description?: string;
  sourceTable?: string;
  parentClassId?: number;
}

// ===== Property =====

export type DataType = "STRING" | "INT" | "DECIMAL" | "DATETIME" | "BOOLEAN";

/**
 * 数据类型下拉选项。
 * labelKey 与 value 一致；组件渲染时通过 ``t(`enums.dataType.${labelKey}`)`` 解析。
 * 保持 types 层无 hook 依赖（避免在纯类型/常量模块触发 React hook 规则问题）。
 */
export const DATA_TYPE_OPTIONS: { value: DataType; labelKey: DataType }[] = [
  { value: "STRING", labelKey: "STRING" },
  { value: "INT", labelKey: "INT" },
  { value: "DECIMAL", labelKey: "DECIMAL" },
  { value: "DATETIME", labelKey: "DATETIME" },
  { value: "BOOLEAN", labelKey: "BOOLEAN" },
];

export interface OntologyProperty {
  id: number;
  classId: number;
  propertyName: string;
  propertyAlias: string | null;
  dataType: string;
  isPrimaryKey: boolean;
  isForeignKey: boolean;
  refClassId: number | null;
  sourceColumn: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface OntologyPropertyCreate {
  classId: number;
  propertyName: string;
  propertyAlias?: string;
  dataType: string;
  isPrimaryKey?: boolean;
  isForeignKey?: boolean;
  refClassId?: number;
  sourceColumn?: string;
}

export interface OntologyPropertyUpdate {
  propertyName?: string;
  propertyAlias?: string;
  dataType?: string;
  isPrimaryKey?: boolean;
  isForeignKey?: boolean;
  refClassId?: number;
  sourceColumn?: string;
}

// ===== Metric =====

export type AggFunction = "SUM" | "AVG" | "COUNT" | "MAX" | "MIN";

export const AGG_FUNCTION_OPTIONS: { value: AggFunction; labelKey: AggFunction }[] = [
  { value: "SUM", labelKey: "SUM" },
  { value: "AVG", labelKey: "AVG" },
  { value: "COUNT", labelKey: "COUNT" },
  { value: "MAX", labelKey: "MAX" },
  { value: "MIN", labelKey: "MIN" },
];

export interface OntologyMetric {
  id: number;
  metricName: string;
  metricAlias: string | null;
  formula: string;
  aggFunction: string;
  targetClassId: number | null;
  dimensionDefaults: Record<string, string> | null;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface OntologyMetricCreate {
  metricName: string;
  metricAlias?: string;
  formula: string;
  aggFunction?: string;
  targetClassId?: number;
  dimensionDefaults?: Record<string, string>;
  createdBy?: string;
}

export interface OntologyMetricUpdate {
  metricName?: string;
  metricAlias?: string;
  formula?: string;
  aggFunction?: string;
  targetClassId?: number;
  dimensionDefaults?: Record<string, string>;
}

// ===== Join（关联关系目录） =====

export type JoinType = "INNER" | "LEFT";
export type RelationType = "foreign_key" | "business";

export const JOIN_TYPE_OPTIONS: { value: JoinType; labelKey: JoinType }[] = [
  { value: "INNER", labelKey: "INNER" },
  { value: "LEFT", labelKey: "LEFT" },
];

export const RELATION_TYPE_OPTIONS: { value: RelationType; labelKey: RelationType }[] = [
  { value: "business", labelKey: "business" },
  { value: "foreign_key", labelKey: "foreign_key" },
];

export interface OntologyJoin {
  id: number;
  sourceClassId: number;
  sourceColumns: string[];
  targetClassId: number;
  targetColumns: string[];
  joinType: string;
  relationType: string;
  description: string | null;
  joinKey: string;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface OntologyJoinCreate {
  sourceClassId: number;
  sourceColumns: string[];
  targetClassId: number;
  targetColumns: string[];
  joinType?: string;
  relationType?: string;
  description?: string;
}

// ===== Semantic Search =====

// 本体实体类型（语义检索命中项的 type 字段）
export type OntologyEntityType = "class" | "property" | "metric";

// 语义检索命中项（对应后端 OntologySearchResult）
export interface OntologySearchHit {
  id: number;
  type: OntologyEntityType;
  name: string;
  alias: string | null;
  description: string | null;
  score: number; // 0~1，越高越相似
}
