/** 本体（Ontology）类型定义 — 对应后端 Pydantic Schema */

// ===== Class =====

/** 业务对象类型（Phase 3.4，采购域 Sheet 03 业务对象目录）。 */
export type ObjectType = "Master" | "Transaction" | "Reference" | "Event";

/**
 * 业务对象类型下拉选项。
 * labelKey 与 value 一致；组件渲染时通过 ``t(`enums.objectType.${labelKey}`)`` 解析。
 * 与 DATA_TYPE_OPTIONS 同模式（types 层无 hook 依赖）。
 */
export const OBJECT_TYPE_OPTIONS: { value: ObjectType; labelKey: ObjectType }[] = [
  { value: "Master", labelKey: "Master" },
  { value: "Transaction", labelKey: "Transaction" },
  { value: "Reference", labelKey: "Reference" },
  { value: "Event", labelKey: "Event" },
];

export interface OntologyClass {
  id: number;
  className: string;
  classAlias: string | null;
  description: string | null;
  sourceTable: string | null;
  parentClassId: number | null;
  /** 治理字段（Phase 3.4）：业务对象类型 / 责任部门或人 */
  objectType: ObjectType | null;
  objectOwner: string | null;
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
  /** 治理字段：null 表示显式不设置（后端落 NULL），undefined 表示不提交该字段 */
  objectType?: ObjectType | null;
  objectOwner?: string | null;
  createdBy?: string;
}

export interface OntologyClassUpdate {
  className?: string;
  classAlias?: string;
  description?: string;
  sourceTable?: string;
  parentClassId?: number;
  /** 治理字段：null 表示显式清空（后端落 NULL），undefined 表示不提交该字段 */
  objectType?: ObjectType | null;
  objectOwner?: string | null;
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
  // 值域（LLM 采纳或人工填入）；null 表示未约束。
  // 后端 OntologyPropertyRead 已暴露（commit 后 B1 起），管理页用它展示「已沉淀」值。
  // 设为可选（mock 测试和旧 client 不传不会触发 tsc 报错）；
  // 生产 API 始终返回该字段（null 或 list[str]）。
  allowedValues?: string[] | null;
  // 列含义描述：后端 OntologyPropertyRead 始终返回；管理页编辑回填 + 列展示用。
  description?: string | null;
  // 约束字段（feat-ontology-property-constraints）：管理页展示 + wizard 初始化
  // adoptedIds。任一字段非空视为已沉淀约束。
  isNotNull?: boolean | null;
  minValue?: string | null;
  maxValue?: string | null;
  regexPattern?: string | null;
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
  // ⚠️ 必须允许 null：后端走 exclude_unset，undefined = 「不修改」。
  // 清空引用类只能靠显式 null，否则用户清掉选择器后旧 ref_class_id 残留。
  refClassId?: number | null;
  sourceColumn?: string;
  // 说明：让管理页可手动修正 LLM 采纳的值。
  // null 表示不修改；空数组 视作清空值域；非空数组 写入 ontology_property.allowed_values。
  allowedValues?: string[] | null;
  description?: string | null;
  // 约束字段（feat-ontology-property-constraints）：管理页手工配置。
  // undefined = 不修改；null = 显式清空（适用 string 类型）。
  // isNotNull 是 boolean；undefined 不修改，false 显式清空，true 写 True。
  isNotNull?: boolean | null;
  minValue?: string | null;
  maxValue?: string | null;
  regexPattern?: string | null;
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

export type MetricType = "MEASURE" | "RATIO" | "DIFFERENCE" | "DERIVED";

export const METRIC_TYPE_OPTIONS: { value: MetricType; labelKey: MetricType }[] = [
  { value: "MEASURE", labelKey: "MEASURE" },
  { value: "RATIO", labelKey: "RATIO" },
  { value: "DIFFERENCE", labelKey: "DIFFERENCE" },
  { value: "DERIVED", labelKey: "DERIVED" },
];

export type MetricAggregationOrder = "WEIGHTED_RATIO" | "AVG_OF_RATIOS" | "NONE";

export const METRIC_AGGREGATION_ORDER_OPTIONS: {
  value: MetricAggregationOrder;
  labelKey: MetricAggregationOrder;
}[] = [
  { value: "WEIGHTED_RATIO", labelKey: "WEIGHTED_RATIO" },
  { value: "AVG_OF_RATIOS", labelKey: "AVG_OF_RATIOS" },
  { value: "NONE", labelKey: "NONE" },
];

export type MetricExecutionSource = "COMPILED" | "ESCAPE_HATCH" | "NONE";

export const METRIC_EXECUTION_SOURCE_OPTIONS: {
  value: MetricExecutionSource;
  labelKey: MetricExecutionSource;
}[] = [
  { value: "COMPILED", labelKey: "COMPILED" },
  { value: "ESCAPE_HATCH", labelKey: "ESCAPE_HATCH" },
  { value: "NONE", labelKey: "NONE" },
];

export type MetricStatus = "DRAFT" | "PUBLISHED";

export const METRIC_STATUS_OPTIONS: { value: MetricStatus; labelKey: MetricStatus }[] = [
  { value: "DRAFT", labelKey: "DRAFT" },
  { value: "PUBLISHED", labelKey: "PUBLISHED" },
];

/**
 * 结构化度量（ontology_metric.numerator_measure / denominator_measure 的 JSONB）。
 *
 * 键名：读端是裸 dict、落库是 `model_dump()`（未带 by_alias）⇒ 恒为 snake_case；
 * 写端 `MeasureSpecSchema`（CamelModel + `populate_by_name=True`）两种键都收。
 * 故前端统一 snake_case。`class_id` / `class_alias` 由回填脚本按本体类推导，
 * 表单不暴露（编辑时原样带回，见 `toMeasureSpec` 的 `existing`）。
 *
 * 后端上限（`schemas.py` `MeasureSpecSchema`）：expr / filter ≤ 1000，
 * class_alias ≤ 100；`extra="forbid"` ⇒ 只能出现这 5 个键。
 */
export interface MeasureSpec {
  agg: string;
  expr: string | null;
  filter: string | null;
  class_id: number | null;
  class_alias: string | null;
}

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
  // ── P1 指标技术形态 ──
  metricType: MetricType;
  numeratorMeasure: MeasureSpec | null;
  denominatorMeasure: MeasureSpec | null;
  aggregationOrder: MetricAggregationOrder | null;
  grainKeys: string[] | null;
  filterExpression: string | null;
  status: MetricStatus;
  owner: string | null;
  executionSource: MetricExecutionSource | null;
}

export interface OntologyMetricCreate {
  metricName: string;
  metricAlias?: string;
  formula: string;
  aggFunction?: string;
  targetClassId?: number;
  dimensionDefaults?: Record<string, string>;
  createdBy?: string;
  // ── P1 指标技术形态 ──
  metricType?: MetricType;
  // 显式 null = 清空（undefined 会被 axios 丢弃 ⇒ 后端静默保留旧值）。
  numeratorMeasure?: MeasureSpec | null;
  denominatorMeasure?: MeasureSpec | null;
  aggregationOrder?: MetricAggregationOrder | null;
  grainKeys?: string[];
  filterExpression?: string | null;
  status?: MetricStatus;
  // `owner` 是用户可编辑字段 ⇒ 清空即「取消负责人」，显式 null 正确。
  // 与 measure 内的 `class_id` / `class_alias`（派生字段，必须保留）刻意不同。
  owner?: string | null;
  executionSource?: MetricExecutionSource | null;
}

export interface OntologyMetricUpdate {
  metricName?: string;
  metricAlias?: string | null;
  formula?: string;
  aggFunction?: string;
  targetClassId?: number | null;
  dimensionDefaults?: Record<string, string> | null;
  // ── P1 指标技术形态 ──
  metricType?: MetricType;
  numeratorMeasure?: MeasureSpec | null;
  denominatorMeasure?: MeasureSpec | null;
  aggregationOrder?: MetricAggregationOrder | null;
  grainKeys?: string[];
  filterExpression?: string | null;
  status?: MetricStatus;
  owner?: string | null;
  executionSource?: MetricExecutionSource | null;
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

// ===== Semantic Relation（类 × 类语义关系） =====

/** 类级语义关系类型（值域与后端 ClassRelationType 对齐；扩展需同步后端 + i18n）。 */
export type SemanticRelationType =
  | "SUPPLIES"
  | "CONTAINS"
  | "GENERATES"
  | "INSPECTED_BY"
  | "GENERATED"
  | "RELATED_TO";

/**
 * 语义关系类型下拉选项（与后端枚举顺序一致）。
 * labelKey 与 value 一致；组件经 ``t(`enums.semanticRelationType.${labelKey}`)`` 解析。
 */
export const SEMANTIC_RELATION_TYPE_OPTIONS: {
  value: SemanticRelationType;
  labelKey: SemanticRelationType;
}[] = [
  { value: "SUPPLIES", labelKey: "SUPPLIES" },
  { value: "CONTAINS", labelKey: "CONTAINS" },
  { value: "GENERATES", labelKey: "GENERATES" },
  { value: "INSPECTED_BY", labelKey: "INSPECTED_BY" },
  { value: "GENERATED", labelKey: "GENERATED" },
  { value: "RELATED_TO", labelKey: "RELATED_TO" },
];

export interface OntologySemanticRelation {
  id: number;
  sourceClassId: number;
  targetClassId: number;
  relationType: SemanticRelationType;
  description: string | null;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface OntologySemanticRelationCreate {
  sourceClassId: number;
  targetClassId: number;
  relationType: SemanticRelationType;
  description?: string;
}

/** 一键补关系结果（POST /ontology/relations/backfill）。 */
export interface RelationBackfillResult {
  syncedJoins: number;
  backfilledReferences: number;
}

// ===== Batch Relation Engine（通用批量关系引擎） =====

/** 已存在关系的冲突处理策略。 */
export type OnConflictPolicy = "skip" | "overwrite";

/** 推断来源：X3 命名约定 or 通用共享列（一方主键）。 */
export type InferBy = "name_convention" | "shared_column";

/** 系统按共享列推断出的物理关联 join 候选。 */
export interface InferredJoin {
  sourceClassId: number;
  sourceClassName: string;
  sourceColumns: string[];
  targetClassId: number;
  targetClassName: string;
  targetColumns: string[];
  relationType: string;
  inferredBy: InferBy;
}

/** 清单（joins + relations），新增/更新关系的载体（JSON 或 CSV 解析产物）。 */
export interface RelationManifest {
  joins: OntologyJoinCreate[];
  relations: OntologySemanticRelationCreate[];
}

/** 批量请求：syncGraph / inferJoins / applyManifest 任选其一或多个。 */
export interface BatchRelationRequest {
  syncGraph?: boolean;
  inferJoins?: boolean;
  applyManifest?: boolean;
  onConflict: OnConflictPolicy;
  manifest?: RelationManifest;
}

/** 行级错误（applyManifest / CSV 解析用）。 */
export interface BatchRowError {
  index: number;
  message: string;
}

/** 某类关系（joins / relations）的处理计数汇总。 */
export interface BatchCounts {
  created: number;
  skipped: number;
  overwritten: number;
  errors: BatchRowError[];
}

/** 本体入图（PG → Neo4j）节点/边计数。 */
export interface GraphSyncResult {
  classes: number;
  properties: number;
  hasPropertyEdges: number;
  referenceEdges: number;
}

/** 批量执行 / 只读预览的统一返回（预览不写库）。 */
export interface BatchRelationResult {
  syncGraph: GraphSyncResult | null;
  inferredJoins: InferredJoin[];
  joins: BatchCounts;
  relations: BatchCounts;
}

/** CSV 清单解析产物（POST /ontology/batch/parse-csv）。 */
export interface OntologyCsvParseResult {
  manifest: RelationManifest;
  errors: BatchRowError[];
}

/** CSV 模板类型（joins / relations）。 */
export type BatchTemplateKind = "joins" | "relations";

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

// ===== Embedding 手动同步（向量对账） =====

export interface EmbeddingSyncFailure {
  classId: number;
  className: string;
  error: string;
}

export interface EmbeddingPropertySyncFailure {
  propertyId: number;
  propertyName: string;
  error: string;
}

export interface EmbeddingSyncMissingResult {
  totalClasses: number;
  missingCount: number;
  syncedCount: number;
  failedCount: number;
  failures: EmbeddingSyncFailure[];
  totalProperties: number;
  missingPropertyCount: number;
  syncedPropertyCount: number;
  failedPropertyCount: number;
  propertyFailures: EmbeddingPropertySyncFailure[];
}

// ===== Neo4j 图谱对账（补图信息） =====

export interface GraphSyncFailure {
  entityType: string;
  entityId: number;
  error: string;
}

export interface GraphSyncMissingResult {
  totalClasses: number;
  missingClassCount: number;
  syncedClassCount: number;
  totalProperties: number;
  missingPropertyCount: number;
  syncedPropertyCount: number;
  totalJoins: number;
  missingJoinCount: number;
  syncedJoinCount: number;
  totalRelations: number;
  missingRelationCount: number;
  syncedRelationCount: number;
  failedCount: number;
  failures: GraphSyncFailure[];
}
