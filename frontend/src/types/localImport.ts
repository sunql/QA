// 本地导入（local import）前端契约类型。
// 字段名与后端 CamelModel JSON 序列化保持一致（snake_case -> camelCase）。

export interface TableFilterRules {
  includeTempTables?: boolean;
  nameBlacklistPatterns?: string[];
}

export interface TypeMappingRules {
  mappings?: Record<string, string>;
}

export interface LlmEnhanceOptions {
  generateAliases?: boolean;
  generateDescriptions?: boolean;
  detectEnums?: boolean;
  suggestFilters?: boolean;
}

export interface JoinInferenceRules {
  // 按数据字典声明的外键推断 join
  inferDeclaredFk?: boolean;
  // 按 Sage X3（THBI）列名约定推断引用边（ITMREF_0 → ITMMASTER 等）
  inferNameConvention?: boolean;
}

export interface ImportRuleConfig {
  tableFilter?: TableFilterRules;
  // 单数 typeMapping，与后端 ImportRuleConfig.type_mapping 的 JSON 契约一致
  typeMapping?: TypeMappingRules;
  llmEnhanceOptions?: LlmEnhanceOptions;
  // 关联关系推断开关（camelCase joinInference，与后端 join_inference 契约一致）
  joinInference?: JoinInferenceRules;
}

// 预览请求：规则 + 表名白名单 + 单表部分列白名单。缺省 selectedTables 表示预览全部表；
// selectedColumns 形如 { PORDERQ: ["POHNUM_0"] }，省略某表表示该表全列导入。
// schema 为 Oracle owner 命名空间（如 THBI）；缺省由后端取连接用户默认 owner。
export interface ImportPreviewRequest {
  rules: ImportRuleConfig;
  selectedTables?: string[] | null;
  selectedColumns?: Record<string, string[]> | null;
  schema?: string | null;
}

export interface ProposedProperty {
  sourceColumn: string;
  propertyName: string;
  propertyAlias: string | null;
  description: string | null;
  dataType: string;
  isPrimaryKey: boolean;
  isForeignKey: boolean;
  enumValues: string[] | null;
}

export interface ProposedClass {
  sourceTable: string;
  className: string;
  classAlias: string | null;
  description: string | null;
  properties: ProposedProperty[];
  isSelected: boolean;
}

export interface ProposedJoin {
  sourceTable: string;
  sourceColumns: string[];
  targetTable: string;
  targetColumns: string[];
  joinType: string;
  relationType: string;
  isSelected: boolean;
  // 推断来源：declared_fk（声明外键）| name_convention（列名约定）；旧响应为 null
  inferredBy?: string | null;
}

export interface ImportConflict {
  type: "class" | "property";
  sourceTable: string | null;
  sourceColumn: string | null;
  existingId: number;
  existingName: string | null;
  proposedName: string | null;
  action: "skip" | "overwrite" | "rename";
}

export interface FilterSuggestions {
  recommendedBlacklistPatterns: string[];
  excludedTables: string[];
}

export interface LlmUsageInfo {
  modelName: string | null;
  promptTokens: number;
  completionTokens: number;
}

export interface ImportPreviewResponse {
  datasourceId: number;
  proposedClasses: ProposedClass[];
  proposedJoins: ProposedJoin[];
  conflicts: ImportConflict[];
  filterSuggestions: FilterSuggestions;
  llmUsage: LlmUsageInfo;
}

export interface ConflictResolution {
  type: "class" | "property";
  existingId: number;
  action: "skip" | "overwrite" | "rename";
  newName?: string | null;
}

export interface ImportExecuteRequest {
  confirmedClasses: ProposedClass[];
  confirmedJoins: ProposedJoin[];
  conflictResolutions: ConflictResolution[];
  syncEmbeddings: boolean;
}

export interface ImportExecuteResponse {
  success: boolean;
  createdClasses: number;
  createdProperties: number;
  createdJoins: number;
  skippedConflicts: number;
  overwrittenConflicts: number;
  errors: Array<{ type: string; name: string; message: string }>;
}
