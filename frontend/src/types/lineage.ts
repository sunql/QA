// 数据血缘 DTO 类型契约（Phase 2.1）
// 与后端 `LineageEdgeCreate / LineageEdgeUpdate / LineageEdgeRead` 1:1 对齐。

export type LineageLayer =
  | "SOURCE_SYSTEM"
  | "ODS"
  | "DWD"
  | "DWS"
  | "ADS"
  | "KPI"
  | "AI";

export type RefreshFrequency = "REALTIME" | "HOURLY" | "DAILY" | "WEEKLY";

export interface LineageEdgeBase {
  sourceLayer: LineageLayer;
  sourceSystem: string;
  sourceObject: string;
  sourceField: string | null;
  targetLayer: LineageLayer;
  targetSystem: string;
  targetObject: string;
  targetField: string | null;
  transformationRule: string | null;
  refreshFrequency: RefreshFrequency;
  owner: string | null;
  description: string | null;
}

export interface LineageEdgeCreate extends LineageEdgeBase {
  // 与后端对齐：refreshFrequency 有默认值 DAILY，DTO 全字段必填由后端决定
}

export interface LineageEdgeUpdate {
  transformationRule?: string | null;
  refreshFrequency?: RefreshFrequency;
  owner?: string | null;
  description?: string | null;
  isActive?: boolean;
}

export interface LineageEdgeRead extends LineageEdgeBase {
  id: number;
  isActive: boolean;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface LineageEdgeListFilter {
  sourceLayer?: LineageLayer;
  targetLayer?: LineageLayer;
  activeOnly?: boolean;
}

/** POST /lineage/edges/extract 响应：本次自动抽取实际新增的血缘边数（幂等，重复调用=0）。 */
export interface LineageExtractResult {
  created: number;
}