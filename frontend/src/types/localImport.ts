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

export interface ImportRuleConfig {
  tableFilter?: TableFilterRules;
  // 单数 typeMapping，与后端 ImportRuleConfig.type_mapping 的 JSON 契约一致
  typeMapping?: TypeMappingRules;
  llmEnhanceOptions?: LlmEnhanceOptions;
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
